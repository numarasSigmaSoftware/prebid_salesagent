"""Webhook URL validation to prevent SSRF attacks.

This module provides security validation for webhook URLs to prevent
Server-Side Request Forgery (SSRF) attacks where malicious users could
trick the server into making requests to internal services.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

from adcp.types import ContextObject, TaskType

from src.core.config import is_production
from src.core.exceptions import AdCPValidationError
from src.core.security.url_validator import check_url_ssrf

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


def resolve_webhook_task_id(request_data: dict[str, Any] | str | None, step_id: str) -> str:
    """Return the buyer-visible task id, falling back for legacy workflow rows.

    A2A persists its outer task id in ``request_data.external_task_id``. MCP/REST
    workflows and older A2A rows do not have that field and continue to use the
    internal workflow step id.
    """
    if isinstance(request_data, dict):
        external_task_id = request_data.get("external_task_id")
        if isinstance(external_task_id, str) and external_task_id:
            return external_task_id
    return step_id


def webhook_ssrf_suggestion() -> str:
    """Buyer-facing suggestion for registration/outbound SSRF rejections."""
    if _strict_mode():
        return WEBHOOK_SSRF_SUGGESTION
    return WEBHOOK_SSRF_SUGGESTION_DEV


def sanitize_webhook_url_for_log(url: str | None) -> str | None:
    """Return ``scheme://host/path`` for logs — never credentials or query."""
    if not url:
        return None
    parsed = urlparse(str(url))
    if parsed.scheme and parsed.hostname:
        return f"{parsed.scheme}://{parsed.hostname}{parsed.path or ''}"
    return None


def webhook_url_for_log(url: str | None) -> str:
    """Total log helper: sanitized URL or the unparseable placeholder (never raw)."""
    return sanitize_webhook_url_for_log(url) or UNPARSEABLE_WEBHOOK_URL_FOR_LOG


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


def reject_unsafe_outbound_webhook_url(
    url: str,
    *,
    log: logging.Logger,
    kind: str,
) -> tuple[bool, str]:
    """Send-time SSRF gate with standardized error logging.

    Returns ``(rejected, error_msg)``. On rejection, logs once with a shared
    message shape so protocol and application delivery paths cannot drift.
    Callers that maintain a circuit breaker should record failure locally.
    """
    is_valid, error_msg = WebhookURLValidator.validate_outbound_webhook_url(url)
    if is_valid:
        return False, ""
    log.error(
        "%s webhook URL failed SSRF validation (url=%s): %s",
        kind,
        webhook_url_for_log(url),
        error_msg,
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

    @staticmethod
    def _require_https_for_webhook() -> bool:
        """The ONE scheme rule for AdCP protocol callbacks — registration and delivery.

        Require HTTPS unless the deployment says EXPLICITLY that it is development.
        Both gates read this, so they cannot disagree about the scheme the way they
        previously did (registration on ``_require_https()``, delivery on
        ``ENVIRONMENT == "development"``), which on the default stack let an ``http://``
        reporting webhook register with a success response and then silently never
        deliver.

        Spec: ``dist/docs/3.1.1/building/by-layer/L1/security.mdx`` requires sellers to
        "Reject non-HTTPS URLs in production" (§Counterparty-supplied URLs, item 1) and
        to enforce "URL parsing, HTTPS, hostname normalization, and reserved-range
        rejection **at write time**" — i.e. at registration, not only at delivery.

        FAIL-SAFE ON UNKNOWN, which is the part two earlier versions of this rule got
        wrong in opposite directions. Keying on ``is_production()`` (a literal ``==
        "production"`` compare) made ``prod``, ``staging``, ``test`` and unset all
        permissive — an ops team writing ``ENVIRONMENT=prod`` would have shipped plaintext
        delivery of callbacks carrying Bearer credentials. Keying on ``_strict_mode()``
        lets ``ADCP_TESTING`` downgrade a production deployment. Only an explicit
        ``development`` relaxes this; anything unrecognised is strict, so a typo fails
        closed rather than open.

        The permissive case must therefore be DECLARED: ``docker-compose.e2e.yml`` sets
        ``ENVIRONMENT: development`` already, and ``docker-compose.yml`` now does too. A
        deployment that declares nothing gets HTTPS enforcement at BOTH gates, so the
        buyer receives an explicit registration rejection instead of a silent delivery
        failure. This is the SCHEME axis of the same single-definition fix
        ``_matches_development_test_host`` applies to the HOST axis.
        """
        return os.getenv("ENVIRONMENT", "").strip().lower() != "development"

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

    @staticmethod
    def _matches_development_test_host(url: str) -> bool:
        """True iff ``url`` is the ONE development-only callback host the seam admits.

        One TERM of the host rule, not the whole rule. Read on its own this helper
        only guarantees the two gates agree about the development test host; the
        ``ADCP_TESTING`` loopback allowance is a second term, applied at registration
        and NOT at protocol delivery. So the symmetry this docstring used to claim —
        "a host admissible for delivery is admissible to register and vice versa" —
        does not hold: measured across ENVIRONMENT x ADCP_TESTING x URL, 5 of 32
        combinations register a callback that delivery then refuses. The direction is
        fail-safe (silent non-delivery, not an SSRF hole) and closing it is tracked
        separately, because the two gates are not interchangeable: this same protocol
        validator is also the buyer-supplied callback gate in create_media_buy, so
        relaxing it to match registration would widen an SSRF boundary.

        Inert outside ``ENVIRONMENT=development``: in production this returns False
        before reading anything else, so no seam can widen the production gate.
        Admits exactly one hostname (the configured value), http/https only, and
        never a URL carrying credentials — arbitrary private hosts stay blocked.
        """
        if os.getenv("ENVIRONMENT", "").lower() != "development":
            return False
        test_host = os.getenv("ADCP_WEBHOOK_TEST_HOST")
        if not test_host:
            return False
        parsed = urlparse(url)
        try:
            _port = parsed.port  # malformed port raises here, not at comparison time
        except ValueError:
            return False
        return (
            parsed.hostname == test_host
            and parsed.scheme in {"http", "https"}
            and parsed.username is None
            and parsed.password is None
        )

    @classmethod
    def validate_webhook_url_registration(cls, url: str) -> tuple[bool, str]:
        """Registration-time SSRF gate (no DNS required).

        Blocks known-bad hostnames and literal private IPs. Unresolvable
        public hostnames are allowed here; send-time re-checks with DNS
        (``validate_outbound_webhook_url``). When ``ADCP_TESTING=true``,
        localhost/loopback are allowed for capture servers. Production
        requires HTTPS.

        Also honors the development-only test-host seam (see
        ``_matches_development_test_host``): the E2E stack's callback host must be
        admissible at REGISTRATION as well as at delivery, or the capture server
        can never be registered in the first place. Both E2E modes pair the
        emitted callback host with this allowed host — ``tests`` in-network
        (docker-compose.e2e.yml), ``host.docker.internal`` standalone
        (tests/e2e/conftest.py).
        """
        allow_localhost = _adcp_testing()
        is_valid, error = check_url_ssrf(
            url,
            resolve_dns=False,
            require_https=cls._require_https_for_webhook(),
        )
        is_valid, error = cls._maybe_allow_localhost(is_valid, error, allow_localhost=allow_localhost)
        if is_valid:
            return True, ""
        if cls._matches_development_test_host(url):
            return True, ""
        return False, error

    @classmethod
    def validate_outbound_webhook_url(cls, url: str) -> tuple[bool, str]:
        """Send-time SSRF gate (full DNS) for APPLICATION webhook delivery.

        Reads the same ``_require_https_for_webhook`` policy as the AdCP callback gates,
        so the scheme decision is one rule across all three delivery sinks. It previously
        short-circuited to ``validate_for_testing`` whenever ``ADCP_TESTING`` was set,
        which dropped HTTPS enforcement entirely — including in production. That left the
        "a testing flag must not downgrade a deployment" property holding at 1 of the 3
        sinks that can deliver the SAME buyer URL with the SAME Bearer token
        (``protocol_webhook_service`` had it; ``webhook_delivery_service`` and
        ``order_approval_service``, both reaching here via
        ``reject_unsafe_outbound_webhook_url``, did not).

        ``ADCP_TESTING`` still governs the HOST axis — capture servers on localhost —
        because that is what the flag is for. It no longer governs the SCHEME axis, which
        is a deployment property.
        """
        is_valid, error = check_url_ssrf(url, require_https=cls._require_https_for_webhook())
        return cls._maybe_allow_localhost(is_valid, error, allow_localhost=_adcp_testing())

    @classmethod
    def validate_protocol_webhook_url(cls, url: str) -> tuple[bool, str]:
        """Validate a protocol callback, with one explicit in-network test seam.

        Production callbacks require HTTPS because notification payloads and
        legacy Bearer credentials must not cross a plaintext connection. The
        E2E Docker stack may use HTTP only for its exact runner hostname while
        in development mode so delivery can be exercised without a public
        relay. Arbitrary private hosts are never enabled by this seam.

        The HTTPS decision is ``_require_https_for_webhook()``, shared with the
        registration gate so the two cannot disagree about the SCHEME — exactly as
        ``_matches_development_test_host`` stops them disagreeing about the HOST.
        See that helper for the divergence this closed and for why the rule keys on
        ``is_production()`` rather than on ``_strict_mode()``.
        """
        is_valid, error = check_url_ssrf(url, require_https=cls._require_https_for_webhook())
        if is_valid:
            return True, ""
        # Same seam definition the registration gate uses — one source of truth, so
        # a host admissible for delivery is admissible to register and vice versa.
        if cls._matches_development_test_host(url):
            return True, ""
        return False, error

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
