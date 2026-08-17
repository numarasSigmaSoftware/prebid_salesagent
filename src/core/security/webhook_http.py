"""Single SSRF-safe HTTP transport for outbound webhook delivery."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from requests.utils import select_proxy

from src.core.bounded_executor import AsyncThreadPoolBulkhead, SyncThreadPoolBulkhead
from src.core.security.url_validator import resolve_and_validate_target
from src.core.webhook_signing_config import load_webhook_signing_config
from src.core.webhook_validator import (
    _allow_private_webhook_targets,
    webhook_url_has_embedded_credentials,
)

# AdCP legacy webhook authentication schemes, spelled exactly as the protocol
# does. ``core/push_notification_config.json`` (v3.1.1) enumerates
# ``['Bearer']`` and ``['HMAC-SHA256']``, and the A2A/REST intake stores
# ``authentication.scheme`` verbatim — so a conformant config is capitalized.
# Comparing against a lowercase literal silently produced an unauthenticated
# delivery, which is why every comparison now goes through ``is_auth_scheme``.
BEARER_AUTH_SCHEME = "Bearer"
HMAC_AUTH_SCHEME = "HMAC-SHA256"
_SIGNATURE_ERROR_RE = re.compile(r'(?:^|,)\s*error\s*=\s*"webhook_[^"]+"', re.IGNORECASE)


@dataclass(frozen=True)
class WebhookHTTPResult:
    """Sanitized response metadata needed for retry classification."""

    status_code: int
    signature_error: bool


def is_signature_auth_failure(status_code: int, www_authenticate: str | None) -> bool:
    """Return true only for the AdCP terminal signature-error 401 taxonomy."""
    if status_code != 401 or not isinstance(www_authenticate, str) or not www_authenticate:
        return False
    scheme, _, parameters = www_authenticate.partition(" ")
    return scheme.casefold() == "signature" and bool(_SIGNATURE_ERROR_RE.search(parameters))


def is_auth_scheme(configured: str | None, scheme: str) -> bool:
    """Match a stored ``authentication_type`` against an AdCP scheme, case-insensitively.

    The comparison is case-insensitive because rows predating the protocol
    spelling exist; the canonical spelling is the constant, not the stored value.
    """
    return configured is not None and configured.casefold() == scheme.casefold()


def validate_webhook_auth_selector(scheme: str | None, credentials: str | None) -> None:
    """Validate the strict default-vs-legacy webhook signing mode selector."""
    if scheme is None:
        if credentials:
            raise ValueError("Webhook credentials require an authentication scheme")
        load_webhook_signing_config(required=True)
        return
    if not (is_auth_scheme(scheme, BEARER_AUTH_SCHEME) or is_auth_scheme(scheme, HMAC_AUTH_SCHEME)):
        raise ValueError(f"Unsupported webhook authentication scheme: {scheme}")
    if not credentials or len(credentials) < 32:
        raise ValueError(f"{scheme} webhook authentication requires credentials of at least 32 characters")


def validate_webhook_config_auth(config: Any) -> None:
    """Validate a dict, Pydantic, or protobuf callback authentication block."""
    authentication = (
        config.get("authentication") if isinstance(config, Mapping) else getattr(config, "authentication", None)
    )
    if isinstance(authentication, Mapping):
        schemes = authentication.get("schemes") or []
        scheme = authentication.get("scheme") or (schemes[0] if schemes else None)
        credentials = authentication.get("credentials")
    else:
        scheme = getattr(authentication, "scheme", None)
        if not scheme:
            schemes = getattr(authentication, "schemes", None)
            scheme = schemes[0] if schemes else None
        credentials = getattr(authentication, "credentials", None)
    validate_webhook_auth_selector(scheme, credentials)


def redact_webhook_url(url: str) -> str:
    """Return only a callback's scheme and authority for safe operational logs."""
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            return "<invalid-webhook-url>"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme.lower()}://{host.lower()}{port}/<redacted>"
    except (TypeError, ValueError):
        return "<invalid-webhook-url>"


def describe_webhook_error(exc: BaseException) -> str:
    """Return a URL- and credential-free network failure description."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return f"{type(exc).__name__} (HTTP {status_code})"
    return type(exc).__name__


WEBHOOK_DNS_TIMEOUT_SECONDS = 2.0

# Retry policy for one outbound webhook target. Kept here, beside the deadline
# that wraps it, because the two are one decision: a deadline shorter than the
# budget it bounds does not "cap" the retries, it CANCELS them.
WEBHOOK_DELIVERY_MAX_RETRIES = 3
WEBHOOK_POST_TIMEOUT_SECONDS = 10.0
WEBHOOK_RETRY_BACKOFF_MAX_JITTER_SECONDS = 1.0


def webhook_retry_delay_seconds(attempt: int, *, jitter: float | None = None) -> float:
    """The ONE definition of the pause before ``attempt``. No sleep before 0.

    Both the loop that sleeps and the deadline that bounds it call this. They
    used to compute it separately — the service hardcoded
    ``(2**attempt) + random.uniform(0, 1)`` while this module re-expressed the
    same formula to derive the deadline — so widening the real backoff to
    ``3**attempt + uniform(0, 5)`` (52s against a 38s deadline, reinstating the
    original defect) left 92 tests passing. A derivation that re-implements
    what it derives from is not a derivation.

    ``jitter=None`` draws the real random component; the deadline passes the
    MAXIMUM so its budget is worst-case rather than a sample.
    """
    if attempt == 0:
        return 0.0
    drawn = random.uniform(0, WEBHOOK_RETRY_BACKOFF_MAX_JITTER_SECONDS) if jitter is None else jitter
    return (2**attempt) + drawn


def _worst_case_delivery_seconds() -> float:
    """Longest one target can legitimately take: every attempt times out.

    Covers EXECUTION only. Admission is separate and deliberately excluded —
    see the deadline comment below.
    """
    posts = WEBHOOK_DELIVERY_MAX_RETRIES * WEBHOOK_POST_TIMEOUT_SECONDS
    backoff = sum(
        webhook_retry_delay_seconds(attempt, jitter=WEBHOOK_RETRY_BACKOFF_MAX_JITTER_SECONDS)
        for attempt in range(1, WEBHOOK_DELIVERY_MAX_RETRIES)
    )
    return posts + backoff


# DERIVED, not chosen. This was a hand-picked 12.0 while the loop it bounds can
# legitimately run 36-38s (3 x 10s POST + 2-3s + 4-5s backoff) — the SECOND
# attempt alone exceeded it. So the TimeoutError branch was not an outlier
# path, it was the normal outcome for any slow endpoint, and it cancelled the
# retries that exist to tolerate exactly that.
#
# The budget covers EXECUTION plus ADMISSION plus DNS. Admission matters
# because the bulkhead starts this clock BEFORE a permit is granted and there
# are only WEBHOOK_DELIVERY_MAX_WORKERS of them: with an execution-only budget
# the 5th concurrent target spends its wait queuing and then has its retries
# cancelled — precisely the defect this deadline was raised to fix, just moved
# from the retry loop to the queue. `_enqueue_and_deliver_target`'s docstring
# claims the deadline "covers admission", so an execution-only figure made
# that claim false.
#
# Admission term: in the worst case every worker is busy for a full execution
# budget before this target is admitted. That is deliberately the pessimistic
# bound — the point is that a queued target is not punished for queuing.
#
# Trade-off, deliberate: a slow target holds a bulkhead worker for up to its
# execution budget. That is the cost of the retry policy actually running.
# Tighten the RETRY POLICY above if that occupancy is too high — do not re-cap
# the deadline below the budget, which just restores the silent cancellation.
WEBHOOK_DELIVERY_DEADLINE_SECONDS = (
    _worst_case_delivery_seconds()  # this target's own retries
    + _worst_case_delivery_seconds()  # worst-case wait for a bulkhead permit
    + WEBHOOK_DNS_TIMEOUT_SECONDS  # the pinning adapter's resolution step
)
WEBHOOK_DELIVERY_MAX_WORKERS = 4
_DELIVERY_DNS_BULKHEAD = SyncThreadPoolBulkhead(
    max_workers=WEBHOOK_DELIVERY_MAX_WORKERS,
    thread_name_prefix="webhook-dns",
)
_ASYNC_WEBHOOK_DELIVERY_BULKHEAD = AsyncThreadPoolBulkhead(
    max_workers=WEBHOOK_DELIVERY_MAX_WORKERS,
    thread_name_prefix="webhook-delivery",
)


class UnsafeWebhookTargetError(requests.RequestException):
    """A webhook target cannot be connected to without violating SSRF policy."""


def _resolve_delivery_target(
    url: str,
    *,
    require_https: bool,
    allow_private: bool,
) -> tuple[str | None, str]:
    """Resolve one target in a bounded DNS bulkhead with a hard deadline."""
    try:
        return _DELIVERY_DNS_BULKHEAD.run(
            resolve_and_validate_target,
            url,
            require_https=require_https,
            allow_private=allow_private,
            timeout_seconds=WEBHOOK_DNS_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise requests.Timeout("Webhook target resolution timed out") from exc


class PinningHTTPAdapter(HTTPAdapter):
    """Resolve, validate, and connect to the same IP for every webhook attempt."""

    def get_connection_with_tls_context(
        self,
        request: requests.PreparedRequest,
        verify: bool | str | None,
        proxies: Mapping[str, str] | None = None,
        cert: Any = None,
    ) -> Any:
        url = request.url or ""
        if webhook_url_has_embedded_credentials(url):
            raise UnsafeWebhookTargetError(
                "Webhook delivery refused: callback URLs must not contain embedded credentials"
            )
        allow_private = _allow_private_webhook_targets()
        pinned_ip, ssrf_error = _resolve_delivery_target(
            url,
            require_https=not allow_private,
            allow_private=allow_private,
        )
        if pinned_ip is None:
            raise UnsafeWebhookTargetError(f"Webhook URL failed SSRF validation: {ssrf_error}")

        # A proxy would resolve the original hostname again and bypass pinning.
        if select_proxy(url, proxies):
            raise UnsafeWebhookTargetError(
                "Webhook delivery refused: a proxy is configured for this target, "
                "which would bypass SSRF connection-pinning. Webhook egress must be direct."
            )

        resolved_verify: bool | str = True if verify is None else verify
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, resolved_verify, cert)
        hostname = host_params["host"]
        host_params["host"] = pinned_ip
        pinned_kwargs: dict[str, Any] = dict(pool_kwargs)
        if host_params["scheme"] == "https":
            # The socket is IP-pinned, while TLS SNI and certificate checks remain
            # bound to the original hostname.
            pinned_kwargs["server_hostname"] = hostname
            pinned_kwargs["assert_hostname"] = hostname
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=cast(Any, pinned_kwargs))


def create_pinned_webhook_session() -> requests.Session:
    """Create a direct-only requests session with the pinning adapter mounted."""
    session = requests.Session()
    session.trust_env = False
    adapter = PinningHTTPAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def webhook_host_header(url: str) -> str:
    """Return requests' canonical HTTP Host without leaking URL userinfo.

    ``requests`` IDNA-encodes Unicode hostnames while preparing the request.
    Build the explicit Host header from that same prepared URL so the header,
    TLS SNI, and certificate hostname all use one ASCII representation.
    """
    prepared = requests.Request(method="POST", url=url).prepare()
    parsed = urlparse(prepared.url or url)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    return f"{hostname}:{parsed.port}" if parsed.port is not None else hostname


def post_webhook_status(
    session: requests.Session,
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> int:
    """POST exact bytes through the pinned transport and return only the status."""
    response = session.post(
        url,
        data=body,
        headers={**headers, "Host": webhook_host_header(url)},
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    try:
        return response.status_code
    finally:
        response.close()


def post_webhook_result(
    session: requests.Session,
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> WebhookHTTPResult:
    """POST exact bytes and retain only safe retry-classification metadata."""
    response = session.post(
        url,
        data=body,
        headers={**headers, "Host": webhook_host_header(url)},
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    try:
        return WebhookHTTPResult(
            status_code=response.status_code,
            signature_error=is_signature_auth_failure(
                response.status_code,
                response.headers.get("WWW-Authenticate"),
            ),
        )
    finally:
        response.close()


async def post_webhook_status_async(
    session: requests.Session,
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
    deadline_seconds: float = WEBHOOK_DELIVERY_DEADLINE_SECONDS,
) -> int:
    """POST off-loop within a capacity-limited end-to-end deadline.

    Caller cancellation never frees a worker permit while the underlying
    synchronous request is still running. This prevents slow DNS or socket I/O
    from turning repeated timeouts into an unbounded default-executor queue.
    """
    try:
        async with asyncio.timeout(deadline_seconds):
            return await _ASYNC_WEBHOOK_DELIVERY_BULKHEAD.run(
                post_webhook_status,
                session,
                url,
                body=body,
                headers=headers,
                timeout=timeout,
            )
    except TimeoutError as exc:
        raise requests.Timeout("Webhook delivery deadline exceeded") from exc


async def post_webhook_result_async(
    session: requests.Session,
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
    deadline_seconds: float = WEBHOOK_DELIVERY_DEADLINE_SECONDS,
) -> WebhookHTTPResult:
    """Async bounded counterpart to :func:`post_webhook_result`."""
    try:
        async with asyncio.timeout(deadline_seconds):
            return await _ASYNC_WEBHOOK_DELIVERY_BULKHEAD.run(
                post_webhook_result,
                session,
                url,
                body=body,
                headers=headers,
                timeout=timeout,
            )
    except TimeoutError as exc:
        raise requests.Timeout("Webhook delivery deadline exceeded") from exc
