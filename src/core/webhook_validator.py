"""Webhook URL validation to prevent SSRF attacks.

This module provides security validation for webhook URLs to prevent
Server-Side Request Forgery (SSRF) attacks where malicious users could
trick the server into making requests to internal services.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from adcp.types import ContextObject, PushNotificationConfig, ReportingWebhook, TaskType

from src.core.config import get_config, is_production
from src.core.exceptions import AdCPValidationError
from src.core.security.url_validator import check_url_ssrf

logger = logging.getLogger(__name__)

# Fallback used when an action label is not a member of the SDK's closed
# TaskType enum. create_mcp_webhook_payload() restricts task_type to that
# enum and would otherwise reject the payload as schema-invalid.
WEBHOOK_TASK_TYPE_FALLBACK = "update_media_buy"

WEBHOOK_SSRF_SUGGESTION = (
    "Provide a public https webhook URL that does not target private, loopback, "
    "link-local, CGNAT, multicast, or cloud-metadata hosts."
)
WEBHOOK_SSRF_SUGGESTION_DEV = (
    "Provide a public http(s) webhook URL that does not target private, loopback, "
    "link-local, CGNAT, multicast, or cloud-metadata hosts."
)

# Log fallback when sanitize_webhook_url_for_log cannot parse scheme/host —
# never fall back to the raw buyer URL (credentials / query).
UNPARSEABLE_WEBHOOK_URL_FOR_LOG = "<unparseable-url>"

# Stands in for a buyer hostname inside an SSRF reason string on its way to a
# log. The hostname can itself be the credential (capability-style URLs), and
# reasons like "Cannot resolve hostname: <host>" embed it verbatim.
REDACTED_HOST_FOR_LOG = "<redacted-host>"


def _adcp_testing() -> bool:
    """True when ADCP_TESTING allows localhost/HTTP for capture servers."""
    return os.environ.get("ADCP_TESTING") == "true"


def _strict_mode() -> bool:
    """Production SSRF policy: HTTPS required and no testing localhost bypass."""
    return is_production() and not _adcp_testing()


def validate_webhook_task_type(task_type: str, fallback: str = WEBHOOK_TASK_TYPE_FALLBACK) -> str:
    """Coerce a task_type to a value accepted by the SDK webhook payload builder.

    ``create_mcp_webhook_payload()`` validates ``task_type`` against the closed
    :class:`adcp.types.TaskType` enum. Action labels sourced from untrusted data
    (e.g. ``workflow_steps.tool_name``) may not be enum members, which would make
    the payload schema-invalid. This helper returns ``task_type`` unchanged when
    it is a valid enum value, otherwise returns ``fallback``.

    This validates ONLY the value destined for the SDK/webhook payload. Callers
    must keep the original action label for internal metadata (audit log,
    delivery-webhook guards, ``WebhookDeliveryLog.task_type``) — see
    salesagent-yi3s.

    Args:
        task_type: The candidate action label.
        fallback: The value to return when ``task_type`` is not a TaskType member.

    Returns:
        ``task_type`` if it is a valid TaskType, otherwise ``fallback``.
    """
    try:
        TaskType(task_type)
    except ValueError:
        return fallback
    return task_type


def webhook_ssrf_suggestion() -> str:
    """Buyer-facing suggestion for registration/outbound SSRF rejections."""
    if _strict_mode():
        return WEBHOOK_SSRF_SUGGESTION
    return WEBHOOK_SSRF_SUGGESTION_DEV


def sanitize_webhook_url_for_log(url: str | None) -> str | None:
    """Return ``scheme://host/path`` for logs — never userinfo or query.

    SUPERSEDED — no production caller remains; use ``redact_webhook_url_for_audit``.
    This form keeps host and path, which for a capability-style delivery URL IS the
    credential, so reintroducing it at a log site would reopen what that redactor
    closed. Kept because its contract is still pinned by tests, and enforced by
    test_superseded_lax_sanitizers_have_no_production_callers.

    Returns None rather than raising on an unparseable URL. ``urlparse`` raises
    ``ValueError: Invalid IPv6 URL`` on inputs like ``https://[::1`` — and this
    runs on the logging path, often while already handling a failure, where a
    raise would replace the error being reported with a parse error from the code
    reporting it. The buyer supplies this string, so malformed input is reachable,
    not hypothetical.

    NOT credential-safe for capability-style delivery URLs, where the credential
    IS the (sub)domain or the path (``https://tok-9fK2z8mQ.hooks.example.com/deliver``):
    both survive this form. Callers that must be safe against that threat model
    pass their own stronger sanitizer to ``reject_unsafe_outbound_webhook_url``
    (see ``redact_webhook_url_for_audit``, which replaces
    host AND path with a keyed digest).
    """
    if not url:
        return None
    try:
        parsed = urlparse(str(url))
    except ValueError:
        return None
    if parsed.scheme and parsed.hostname:
        return f"{parsed.scheme}://{parsed.hostname}{parsed.path or ''}"
    return None


def webhook_url_for_log(url: str | None) -> str:
    """Total log helper: sanitized URL or the unparseable placeholder (never raw)."""
    return sanitize_webhook_url_for_log(url) or UNPARSEABLE_WEBHOOK_URL_FOR_LOG


# The HMAC context string is FROZEN at its original value. It names the function's
# former home (protocol_webhook_service), which now reads as a misnomer — but it is
# an input to every digest ever written to WebhookDeliveryLog.webhook_url, so
# rewording it would silently re-key the whole column and orphan every historical
# row from the URL it identifies. The stale-looking name is the point: it is a
# version tag, not a description.
_AUDIT_REDACTION_CONTEXT = b"protocol_webhook_service.redact_webhook_url_for_audit.v4"


def redact_webhook_url_for_audit(url: str) -> str:
    """Return a non-reversible audit form of *url*, for log output AND durable storage.

    Keeps only ``scheme://<redacted:key_id:hmac>`` — nothing about the buyer-supplied
    host, port, path, query, or fragment survives. Two earlier versions of this
    function kept the hostname on the theory that a hostname can't carry a
    credential; that assumption is wrong for capability-style delivery URLs, where
    the credential IS the (sub)domain (e.g. ``https://tok-9fK2z8mQ.hooks.example.com/deliver``)
    — a private/unique or otherwise unclassifiable subdomain is exactly as
    unconstrained as the path or query, so it gets the same treatment.

    The digest is an HMAC-SHA256 (never a bare hash) keyed with a DEDICATED secret,
    ``AppConfig.webhook_audit_hmac_key`` — deliberately not ``flask_secret_key``.
    Reusing the session-signing key would make this correlation identifier a
    hostage of routine session-key rotation (rotate the session key, and every
    historical ``WebhookDeliveryLog`` row becomes unrecognizable), and
    ``flask_secret_key`` ships a public, unvalidated dev default that a
    misconfigured production deployment could silently inherit. The dedicated key
    is required and length-checked in production by ``validate_configuration()``.
    Truncated to 128 bits, so two log lines or DB rows can be recognized as the
    same target without exposing it — but unlike an unkeyed digest, it can't be
    matched offline against a dictionary of guessed URLs (low-entropy webhook URLs
    are a real threat model an unkeyed hash doesn't defend against).

    The key ID (``AppConfig.webhook_audit_hmac_key_id``, default ``"v1"``) is folded
    into the HMAC input for domain separation AND written into the output in the
    clear, so a key rotation doesn't just silently break correlation for every row
    written under the old key — the row's own audit identifier still says which key
    generation produced it.

    ``validate_configuration()`` requires a real key in production, but a blank
    (or whitespace-only) key is legal OUTSIDE production — the same shared
    staging/dev environments that skip it can still hold real buyer URLs and
    write real ``WebhookDeliveryLog`` rows, so this function must not silently
    degrade to an HMAC keyed with an empty string: that would produce a value
    that *looks* like a real per-URL digest while being exactly as guessable as
    an unkeyed hash (zero secret material). Instead it emits a constant,
    non-correlating placeholder (``scheme://<redacted>``, no digest, no key ID)
    — honestly offering no correlation rather than a fake one.

    Used both for log lines and for the value persisted to ``WebhookDeliveryLog.webhook_url``
    (pure audit data — never read back to dial a real request). Never use this on the URL
    passed to ``requests`` for the actual outbound call — that one needs the untouched original.
    """
    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return "REDACTED"
        config = get_config()
        raw_key = config.webhook_audit_hmac_key
        if not raw_key.strip():
            return f"{parsed.scheme}://<redacted>"
        key_id = config.webhook_audit_hmac_key_id
        message = _AUDIT_REDACTION_CONTEXT + b":" + key_id.encode() + b":" + url.encode()
        digest = hmac.new(raw_key.encode(), message, hashlib.sha256).hexdigest()[:32]
        return f"{parsed.scheme}://<redacted:{key_id}:{digest}>"
    except Exception:
        # Fails CLOSED — never the raw URL — but not silently: every audit row and
        # log line degrades to the same constant, so without this the whole column
        # can lose its correlation value (a misconfigured key, say) with nothing
        # anywhere saying why. debug, not warning: on a malformed buyer URL this is
        # the expected outcome, and the caller already records the rejection.
        logger.debug("Webhook URL redaction fell back to the constant placeholder", exc_info=True)
        return "REDACTED"


def reject_unsafe_webhook_registration_url(
    url: str | None,
    *,
    field: str,
    context: ContextObject | dict[str, Any] | None = None,
) -> None:
    """Raise AdCPValidationError when ``url`` fails the registration SSRF gate.

    Blank / whitespace-only / ``None`` URLs are a no-op (not a rejection) so
    callers can extract-then-call unconditionally.
    """
    if url is None or not str(url).strip():
        return
    is_valid, error_msg = WebhookURLValidator.validate_webhook_url_registration(str(url))
    if not is_valid:
        raise AdCPValidationError(
            f"Invalid {field}: {error_msg}",
            field=field,
            suggestion=webhook_ssrf_suggestion(),
            recovery="correctable",
            context=context,
        )


def reject_unsafe_registration_source_url(
    url_source: ReportingWebhook | PushNotificationConfig | Mapping[str, Any] | None,
    *,
    field: str,
    context: ContextObject | dict[str, Any] | None = None,
) -> str | None:
    """Extract ``.url`` from a webhook-registration-shaped source, validate it, return it.

    ``url_source`` is whatever the caller has on hand for a ``reporting_webhook``
    or ``push_notification_config`` field — a typed adcp model (``.url``
    attribute) on some paths, a raw mapping (``["url"]``) on others. Single call
    site for the "extract url, stringify, validate" pattern repeated at every
    registration point across create, update, and creative sync, on both fields,
    so drift (someone forgets the URL check on a new field or a new call site)
    has one place to be caught rather than N independently-copied bodies. A
    ``None`` source is a no-op, matching ``reject_unsafe_webhook_registration_url``'s
    own None-is-a-no-op contract.

    The parameter names the concrete sources rather than ``object`` on purpose:
    extraction falls back to ``None`` for anything lacking a ``url``, and a
    ``None`` url is a documented no-op, so an ``object`` annotation let a wrong
    argument silently SKIP this security gate with neither a type error nor a
    runtime signal.

    Returns the extracted URL (stringified; ``None`` when the source is ``None``
    or carries no url) so a caller that must also log or persist it reuses this
    one extraction rather than re-deriving it — see ``creatives/_sync.py``.
    """
    if url_source is None:
        return None
    raw_url = url_source.get("url") if isinstance(url_source, Mapping) else getattr(url_source, "url", None)
    url = str(raw_url) if raw_url is not None else None
    reject_unsafe_webhook_registration_url(url, field=field, context=context)
    return url


def reject_unsafe_outbound_webhook_url(
    url: str,
    *,
    log: logging.Logger,
    kind: str,
    sanitize: Callable[[str], str],
) -> tuple[bool, str]:
    """Send-time SSRF gate with standardized error logging.

    Returns ``(rejected, error_msg)``. On rejection, logs once with a shared
    message shape so protocol and application delivery paths cannot drift.
    Callers that maintain a circuit breaker should record failure locally.

    ``sanitize`` renders the URL for the log line. It is REQUIRED and has no
    default: the two choices fail in opposite directions — the lax form leaks a
    capability-style URL's credential-bearing host, and the strong form costs an
    operator the hostname while debugging — so no default is safe in both, and a
    silently-inherited one is exactly how two of the three gates ended up laxer
    than the path they guard. Callers must state which threat model applies.

    It is a parameter because a
    caller whose threat model treats the hostname itself as a credential (the
    capability-style delivery URL case — see ``redact_webhook_url_for_audit`` in
    protocol_webhook_service) must not have this failure path log the very
    thing it redacts everywhere else. The rejection branch fires precisely for
    hostile or misconfigured URLs, so it is the *last* place that should be
    laxer than the success path.

    ``error_msg`` is returned to the caller intact (it becomes the buyer-facing
    validation message, and the buyer already knows their own URL) but is
    scrubbed of the raw hostname before it reaches the log — several SSRF
    reasons embed it verbatim (``Cannot resolve hostname: <host>``), which would
    otherwise re-leak through the reason string whatever ``sanitize`` removed
    from the url field.
    """
    is_valid, error_msg = WebhookURLValidator.validate_outbound_webhook_url(url)
    if is_valid:
        return False, ""
    # This branch fires precisely for hostile or malformed URLs, so it is the last
    # place that may raise: urlparse rejects a malformed IPv6 literal such as
    # https://[::1 with ValueError, which would replace the SSRF rejection the
    # caller is waiting on — (rejected, reason) — with an exception from the code
    # doing the rejecting, and the caller would never learn the URL was unsafe.
    try:
        hostname = urlparse(str(url)).hostname
    except ValueError:
        hostname = None
    loggable_reason = error_msg.replace(hostname, REDACTED_HOST_FOR_LOG) if hostname else error_msg
    log.error(
        "%s webhook URL failed SSRF validation (url=%s): %s",
        kind,
        sanitize(url),
        loggable_reason,
    )
    return True, error_msg


class WebhookURLValidator:
    """Validates webhook URLs to prevent SSRF attacks."""

    @staticmethod
    def _maybe_allow_localhost(is_valid: bool, error: str, *, allow_localhost: bool) -> tuple[bool, str]:
        """Override localhost/loopback SSRF failures when testing allows them."""
        if not is_valid and allow_localhost:
            if "localhost" in error.lower() or "127.0.0" in error or "loopback" in error.lower():
                return True, ""
        return is_valid, error

    @staticmethod
    def _require_https() -> bool:
        """Production requires HTTPS; ADCP_TESTING keeps HTTP for capture servers."""
        return _strict_mode()

    @classmethod
    def validate_webhook_url(cls, url: str) -> tuple[bool, str]:
        """
        Validate webhook URL for SSRF protection.

        Args:
            url: The webhook URL to validate

        Returns:
            (is_valid, error_message) - is_valid is True if safe, error_message explains failures
        """
        return check_url_ssrf(url, require_https=cls._require_https())

    @classmethod
    def validate_webhook_url_registration(cls, url: str) -> tuple[bool, str]:
        """Registration-time SSRF gate (no DNS required).

        Blocks known-bad hostnames and literal private IPs. Unresolvable
        public hostnames are allowed here; send-time re-checks with DNS
        (``validate_outbound_webhook_url``). When ``ADCP_TESTING=true``,
        localhost/loopback are allowed for capture servers. Production
        requires HTTPS.
        """
        allow_localhost = _adcp_testing()
        is_valid, error = check_url_ssrf(
            url,
            resolve_dns=False,
            require_https=cls._require_https(),
        )
        return cls._maybe_allow_localhost(is_valid, error, allow_localhost=allow_localhost)

    @classmethod
    def validate_outbound_webhook_url(cls, url: str) -> tuple[bool, str]:
        """Send-time SSRF gate (full DNS), with localhost allowance under ADCP_TESTING."""
        if _adcp_testing():
            return cls.validate_for_testing(url, allow_localhost=True)
        return cls.validate_webhook_url(url)

    @classmethod
    def validate_for_testing(cls, url: str, allow_localhost: bool = False) -> tuple[bool, str]:
        """
        Validate webhook URL with optional localhost allowance for testing.

        This is useful for development/testing scenarios where webhooks need to
        point to localhost services. Production should use validate_webhook_url().

        Args:
            url: The webhook URL to validate
            allow_localhost: If True, allows localhost and 127.0.0.1

        Returns:
            (is_valid, error_message)
        """
        # Testing path always allows HTTP (capture servers, local harnesses).
        is_valid, error = check_url_ssrf(url, require_https=False)
        return cls._maybe_allow_localhost(is_valid, error, allow_localhost=allow_localhost)
