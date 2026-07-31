"""SSRF gate for protocol push / reporting webhook URLs.

Pins that ProtocolWebhookService refuses unsafe URLs before any outbound POST,
mirrors application-level WebhookURLValidator usage in webhook_delivery, and
covers registration wiring: create_media_buy, sync_creatives, A2A message/send,
and A2A set_push_notification_config handler.

Wire-level VALIDATION_ERROR / recovery=correctable + suggestion for
create_media_buy and sync_creatives is graded by transport-blind BDD scenarios
(BR-UC-002-ext-webhook-ssrf, BR-UC-006-ext-webhook-ssrf). A2A-native push-config
endpoints translate the same registration gate to InvalidParamsError with the
AdCP VALIDATION_ERROR envelope in ``data`` — pinned below.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from a2a.types import (
    InvalidParamsError,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskPushNotificationConfig,
)
from adcp.types import ReportingWebhook

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler, _reject_unsafe_a2a_webhook_url
from src.core.database.models import PushNotificationConfig
from src.core.exceptions import AdCPValidationError
from src.core.resolved_identity import ResolvedIdentity
from src.core.schemas import CreateMediaBuyRequest
from src.core.testing_hooks import AdCPTestContext
from src.core.tools.creatives._sync import _sync_creatives_impl
from src.core.tools.media_buy_create import _create_media_buy_impl
from src.core.webhook_validator import (
    WEBHOOK_SSRF_SUGGESTION_DEV,
    WebhookURLValidator,
    reject_unsafe_webhook_registration_url,
)
from src.services.protocol_webhook_service import ProtocolWebhookService
from tests.factories.principal import PrincipalFactory
from tests.helpers import assert_envelope_shape
from tests.helpers.adcp_factories import create_test_media_buy_request_dict, valid_reporting_webhook

# https:// on purpose: an http:// URL lets the scheme check reject before the host is
# ever examined, so the test would pass without the host defence running at all.
#
# Note what this does NOT establish. check_url_ssrf blocks hosts in two independent
# layers — BLOCKED_HOSTNAMES (a string set) and _blocked_ip_error (networks + the
# is_private/is_loopback/is_link_local predicates) — and both of these URLs are caught
# by the HOSTNAME layer, so disabling the IP layer entirely still leaves them green.
# Switching to https removes the scheme short-circuit; making either individual layer
# load-bearing needs a host that only that layer catches, which is separate work.
_METADATA_URL = "https://169.254.169.254/latest/meta-data/"


def _config(url: str) -> PushNotificationConfig:
    return PushNotificationConfig(
        id="pnc-ssrf-test",
        tenant_id="t1",
        principal_id="p1",
        url=url,
        authentication_type=None,
        authentication_token=None,
        is_active=True,
    )


def _reporting_webhook(url: str) -> ReportingWebhook:
    return ReportingWebhook.model_validate(valid_reporting_webhook(url))


def _identity() -> ResolvedIdentity:
    return PrincipalFactory.make_identity(
        principal_id="principal_1",
        tenant_id="test_tenant",
        auth_token="test-token",
        protocol="mcp",
        tenant={"tenant_id": "test_tenant", "human_review_required": False, "auto_create_media_buys": True},
        testing_context=AdCPTestContext(dry_run=False, test_session_id="test-session"),
    )


def _minimal_create_request(**overrides):
    data = create_test_media_buy_request_dict(
        product_ids=["prod_1"],
        total_budget=5000.0,
        pricing_option_id="cpm_usd_fixed",
        idempotency_key="unit-ssrf-create-key-0001",
        **overrides,
    )
    return CreateMediaBuyRequest(**data)


@pytest.mark.asyncio
async def test_send_notification_rejects_metadata_url_without_post() -> None:
    """Cloud metadata URL must fail closed before requests.Session.post."""
    service = ProtocolWebhookService()
    with patch.object(service._session, "post", autospec=True) as mock_post:
        sent = await service.send_notification(
            _config(_METADATA_URL),
            payload={"task_id": "t1", "status": "completed"},
            metadata={"task_type": "create_media_buy"},
        )
    assert sent is False
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_send_notification_rejects_localhost_without_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production send path must reject localhost (ADCP_TESTING off)."""
    monkeypatch.delenv("ADCP_TESTING", raising=False)
    service = ProtocolWebhookService()
    with patch.object(service._session, "post", autospec=True) as mock_post:
        sent = await service.send_notification(
            _config("https://localhost:9999/webhook"),  # https: blocklist is the sole rejector
            payload={"task_id": "t1", "status": "completed"},
            metadata={"task_type": "create_media_buy"},
        )
    assert sent is False
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_send_notification_posts_when_url_is_public() -> None:
    """Safe public URL proceeds to POST (validator + session both exercised)."""
    service = ProtocolWebhookService()
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()

    with (
        # Patches the validator this branch's delivery path actually calls. #1697 gated
        # sends with the generic validate_outbound_webhook_url; this branch gates
        # per-attempt inside the retry loop with validate_protocol_webhook_url — the
        # protocol-callback validator, which carries the ADCP_WEBHOOK_TEST_HOST dev seam
        # the e2e capture server needs. Intent of this test is unchanged: stub the gate
        # open so the POST path itself is what's exercised.
        patch(
            "src.core.webhook_validator.WebhookURLValidator.validate_protocol_webhook_url",
            return_value=(True, ""),
        ),
        patch.object(service._session, "post", return_value=response) as mock_post,
        patch(
            "src.services.protocol_webhook_service.extract_webhook_result_data",
            return_value=None,
        ),
        patch(
            "src.services.protocol_webhook_service.get_audit_logger",
            return_value=MagicMock(),
        ),
    ):
        sent = await service.send_notification(
            _config("https://buyer.example.com/hooks/adcp"),
            payload={"task_id": "t1", "status": "completed"},
            metadata={"task_type": "create_media_buy", "tenant_id": "t1"},
        )

    assert sent is True
    mock_post.assert_called_once_with(
        "https://buyer.example.com/hooks/adcp",
        json={"task_id": "t1", "status": "completed"},
        headers={"Content-Type": "application/json", "User-Agent": "AdCP-Sales-Agent/1.0"},
        timeout=10.0,
        allow_redirects=False,
    )


@pytest.mark.asyncio
async def test_send_notification_does_not_follow_redirect_to_metadata() -> None:
    """302 to link-local metadata must not be followed (open-redirect SSRF)."""
    service = ProtocolWebhookService()
    redirect = MagicMock()
    redirect.status_code = 302
    redirect.headers = {"Location": _METADATA_URL}
    redirect.raise_for_status.side_effect = requests.HTTPError("302 redirect")

    with (
        # Patches the validator this branch's delivery path actually calls. #1697 gated
        # sends with the generic validate_outbound_webhook_url; this branch gates
        # per-attempt inside the retry loop with validate_protocol_webhook_url — the
        # protocol-callback validator, which carries the ADCP_WEBHOOK_TEST_HOST dev seam
        # the e2e capture server needs. Intent of this test is unchanged: stub the gate
        # open so the POST path itself is what's exercised.
        patch(
            "src.core.webhook_validator.WebhookURLValidator.validate_protocol_webhook_url",
            return_value=(True, ""),
        ),
        patch.object(service._session, "post", return_value=redirect) as mock_post,
        patch(
            "src.services.protocol_webhook_service.extract_webhook_result_data",
            return_value=None,
        ),
        patch(
            "src.services.protocol_webhook_service.get_audit_logger",
            return_value=MagicMock(),
        ),
        patch("src.services.protocol_webhook_service.asyncio.sleep", return_value=None),
    ):
        sent = await service.send_notification(
            _config("https://buyer.example.com/hooks/adcp"),
            payload={"task_id": "t1", "status": "completed"},
            metadata={"task_type": "create_media_buy", "tenant_id": "t1"},
        )

    assert sent is False
    assert mock_post.call_count >= 1
    for call in mock_post.call_args_list:
        assert call.kwargs.get("allow_redirects") is False
        assert call.args[0] == "https://buyer.example.com/hooks/adcp"


def test_reject_unsafe_webhook_registration_url_raises_validation_error() -> None:
    with pytest.raises(AdCPValidationError) as exc_info:
        reject_unsafe_webhook_registration_url(
            "http://metadata.google.internal/computeMetadata/v1/",
            field="reporting_webhook.url",
        )
    assert exc_info.value.field == "reporting_webhook.url"
    assert "Invalid reporting_webhook.url" in exc_info.value.message
    assert exc_info.value.suggestion == WEBHOOK_SSRF_SUGGESTION_DEV
    assert exc_info.value.recovery == "correctable"


@pytest.mark.parametrize("callback_host", ["tests", "host.docker.internal"])
def test_registration_admits_the_configured_e2e_callback_host(monkeypatch, callback_host: str) -> None:
    """The E2E callback host must survive the create_media_buy registration gate.

    Drives the exact production helper both create_media_buy call sites use for
    reporting_webhook.url and push_notification_config.url. When this gate did not
    consult the development-only test-host seam, every E2E webhook flow died here
    — before any delivery was attempted — with VALIDATION_ERROR on those fields.

    Both configured pairings are covered: "tests" for the in-network runner
    (docker-compose.e2e.yml) and "host.docker.internal" for the standalone
    fixture (tests/e2e/conftest.py), so neither CI mode can regress alone.
    """
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("ADCP_TESTING", "true")
    monkeypatch.setenv("ADCP_WEBHOOK_TEST_HOST", callback_host)

    for field in ("reporting_webhook.url", "push_notification_config.url"):
        # Does not raise — that IS the assertion (the helper's contract is
        # raise-or-return-None, so a rejection surfaces as AdCPValidationError).
        reject_unsafe_webhook_registration_url(
            f"http://{callback_host}:9999/webhook",
            field=field,
        )


def test_registration_seam_does_not_admit_other_hosts_in_development(monkeypatch) -> None:
    """Admitting the E2E callback host must not admit private hosts generally."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("ADCP_TESTING", "true")
    monkeypatch.setenv("ADCP_WEBHOOK_TEST_HOST", "host.docker.internal")

    with pytest.raises(AdCPValidationError):
        reject_unsafe_webhook_registration_url(
            "http://metadata.google.internal/computeMetadata/v1/",
            field="reporting_webhook.url",
        )


# EXPECTED VALUE per deployment shape — not "the two gates agree".
#
# An agreement-only assertion is what let a regression ship: unifying the two gates onto
# is_production() dropped HTTPS enforcement on DELIVERY for staging/test/prod/unset (5 of
# 7 shapes, where the previous rule was strict), and a test asserting only that the gates
# MATCH cannot see a change that moves both. Every row below therefore states the value.
#
# The rule is "HTTPS unless EXPLICITLY development", so every unrecognised or absent value
# is strict: `prod` is the sharp one — it reads as production to an operator and is not
# the literal `production` that is_production() compares against.
#
# ADCP_TESTING is varied deliberately and never changes a row: a testing flag must not be
# able to downgrade a deployment to plaintext for callbacks carrying Bearer credentials.
_ENV_MATRIX = [
    pytest.param(None, None, True, id="unset-strict"),
    pytest.param(None, "true", True, id="unset-testing-still-strict"),
    pytest.param("", None, True, id="empty-strict"),
    pytest.param("development", None, False, id="development-permissive"),
    pytest.param("development", "true", False, id="development-testing-permissive"),
    pytest.param("DEVELOPMENT", None, False, id="development-case-insensitive"),
    pytest.param("test", None, True, id="test-strict"),
    pytest.param("staging", None, True, id="staging-strict"),
    pytest.param("staging", "true", True, id="staging-testing-still-strict"),
    pytest.param("prod", None, True, id="prod-abbrev-strict"),
    pytest.param("prod", "true", True, id="prod-abbrev-testing-still-strict"),
    pytest.param("production", None, True, id="production-strict"),
    pytest.param("production", "true", True, id="production-testing-still-strict"),
]


def _requires_https(gate, host: str) -> bool:
    """Whether ``gate`` admits ``https://host`` but refuses ``http://host``.

    Isolates the SCHEME axis. The two gates deliberately differ on the DNS axis —
    registration passes ``resolve_dns=False`` so an unresolvable public hostname is
    admissible at registration and re-checked at send time — so comparing their raw
    verdicts on one URL measures DNS, not scheme (an earlier draft of this test did
    exactly that and "failed" identically with and without the fix). Holding the host
    fixed and flipping only the scheme cancels every other axis out.
    """
    https_ok, _ = gate(f"https://{host}/webhook")
    http_ok, _ = gate(f"http://{host}/webhook")
    assert https_ok, f"{gate.__name__} rejected https://{host} — the probe host must clear every other axis"
    return not http_ok


@pytest.mark.parametrize("environment,adcp_testing,expect_https_required", _ENV_MATRIX)
def test_callback_scheme_policy_per_environment(monkeypatch, environment, adcp_testing, expect_https_required) -> None:
    """Both callback gates enforce the EXPECTED scheme for every deployment shape.

    Two properties in one assertion, and the second is the one a previous version of this
    test lacked:

    1. Registration and delivery agree — the host axis has ``_matches_development_test_host``
       keeping them on one definition, and this is the scheme equivalent. Without it, an
       ``http://`` reporting webhook registered with a success response and then silently
       never delivered on the default stack.
    2. They agree on the RIGHT value. The previous version asserted only that the two gates
       MATCH, which cannot observe a change that moves both — and one did: unifying onto
       ``is_production()`` dropped HTTPS enforcement on delivery for staging/test/prod/unset,
       5 of the 7 shapes it covered, with every test still green.

    ``ADCP_TESTING`` is varied across otherwise-identical rows and never changes the
    expectation: a testing flag must not downgrade a deployment to plaintext for callbacks
    carrying notification payloads and legacy Bearer credentials.
    """
    for var, value in (("ENVIRONMENT", environment), ("ADCP_TESTING", adcp_testing)):
        monkeypatch.delenv(var, raising=False)
        if value is not None:
            monkeypatch.setenv(var, value)
    monkeypatch.delenv("ADCP_WEBHOOK_TEST_HOST", raising=False)

    # A resolvable public host, so DNS (the axis the gates legitimately differ on) is
    # satisfied for both and only the scheme varies.
    host = "reporting.example.com"
    with patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"):
        register_strict = _requires_https(WebhookURLValidator.validate_webhook_url_registration, host)
        deliver_strict = _requires_https(WebhookURLValidator.validate_protocol_webhook_url, host)

    context = f"ENVIRONMENT={environment!r} ADCP_TESTING={adcp_testing!r}"
    assert register_strict == deliver_strict, (
        f"{context}: registration {'requires HTTPS' if register_strict else 'accepts http'} but "
        f"delivery {'requires HTTPS' if deliver_strict else 'accepts http'}. A callback that "
        f"registers and never delivers is invisible to the buyer."
    )
    assert register_strict is expect_https_required, (
        f"{context}: expected HTTPS to be {'REQUIRED' if expect_https_required else 'optional'} "
        f"at both callback gates, got {'required' if register_strict else 'optional'}. Only an "
        f"explicit 'development' may relax this — anything unrecognised fails closed."
    )


# The three sinks that can deliver the SAME buyer URL with the SAME Bearer token. They
# reach different validators, which is exactly how the scheme policy drifted apart.
_DELIVERY_SINKS = [
    pytest.param("validate_protocol_webhook_url", id="adcp-callback-delivery"),
    pytest.param("validate_webhook_url_registration", id="adcp-callback-registration"),
    # webhook_delivery_service and order_approval_service both arrive here via
    # reject_unsafe_outbound_webhook_url.
    pytest.param("validate_outbound_webhook_url", id="application-delivery"),
]


@pytest.mark.parametrize("gate_name", _DELIVERY_SINKS)
@pytest.mark.parametrize("environment", ["production", "prod", "staging", None], ids=lambda v: f"env-{v}")
def test_adcp_testing_never_downgrades_a_non_development_deployment(monkeypatch, gate_name, environment) -> None:
    """A testing flag must not relax the SCHEME on any delivery sink.

    ``ADCP_TESTING`` exists to admit localhost capture servers — a HOST-axis allowance.
    It used to also drop HTTPS enforcement entirely on ``validate_outbound_webhook_url``,
    which short-circuited to ``validate_for_testing(require_https=False)`` whenever the
    flag was set. Since a deployment can carry that flag, a production deployment could
    ship plaintext delivery of callbacks carrying Bearer credentials.

    The property held at ONE of these three sinks and not the other two, for the same
    buyer URL — which is why this is parametrized over the sinks rather than asserted on
    whichever one a given test happened to reach.
    """
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    if environment is not None:
        monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("ADCP_TESTING", "true")
    monkeypatch.delenv("ADCP_WEBHOOK_TEST_HOST", raising=False)

    gate = getattr(WebhookURLValidator, gate_name)
    with patch("src.core.security.url_validator.socket.gethostbyname", return_value="93.184.216.34"):
        accepts_plaintext, _ = gate("http://buyer.example.com/adcp-callback")

    assert not accepts_plaintext, (
        f"{gate_name} accepted a plaintext callback with ENVIRONMENT={environment!r} because "
        f"ADCP_TESTING was set. That flag governs the HOST axis (localhost capture servers), "
        f"never the scheme — these callbacks carry notification payloads and Bearer tokens."
    )


@pytest.mark.parametrize("adcp_testing", [None, "true"], ids=["no-testing-flag", "testing-flag-set"])
def test_production_rejects_plaintext_callbacks_at_both_gates(monkeypatch, adcp_testing) -> None:
    """Unifying the scheme policy must not weaken real production.

    The pairing above only proves the two gates AGREE; this pins WHERE they agree for
    the one deployment that matters, so agreement cannot be satisfied by making both
    gates permissive.

    Parametrized over ``ADCP_TESTING`` on purpose, and that is the load-bearing half.
    A first attempt at this fix unified both gates onto ``_require_https()``
    (``_strict_mode()`` = production AND NOT ADCP_TESTING) on the reasoning that the
    protocol gate was the odd one out of three. That direction is backwards for a
    security gate: "the other two are permissive" argues for tightening them, not for
    loosening the strict one. It let a testing flag downgrade a production deployment
    to plaintext for callbacks carrying notification payloads and legacy Bearer
    credentials. The whole unit suite sets ADCP_TESTING=true via an autouse fixture, so
    that cell is exactly where the weakening would hide.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("ADCP_TESTING", raising=False)
    if adcp_testing is not None:
        monkeypatch.setenv("ADCP_TESTING", adcp_testing)
    monkeypatch.setenv("ADCP_WEBHOOK_TEST_HOST", "host.docker.internal")

    for url in ("http://reporting.example.com/webhook", "http://host.docker.internal:9999/webhook"):
        registers, _ = WebhookURLValidator.validate_webhook_url_registration(url)
        delivers, _ = WebhookURLValidator.validate_protocol_webhook_url(url)
        assert not registers, f"production must not register plaintext {url} (ADCP_TESTING={adcp_testing!r})"
        assert not delivers, f"production must not deliver to plaintext {url} (ADCP_TESTING={adcp_testing!r})"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_reject_unsafe_webhook_registration_url_noop_on_blank(blank: str | None) -> None:
    """Blank / missing URL is not a rejection — callers extract-then-call unconditionally."""
    reject_unsafe_webhook_registration_url(blank, field="push_notification_config.url")


def test_sanitize_webhook_url_for_log_strips_credentials_query_and_fragment() -> None:
    from src.core.webhook_validator import (
        UNPARSEABLE_WEBHOOK_URL_FOR_LOG,
        sanitize_webhook_url_for_log,
        webhook_url_for_log,
    )

    dirty = "https://user:pass@buyer.example.com:8443/hook?token=abc#frag"
    assert sanitize_webhook_url_for_log(dirty) == "https://buyer.example.com/hook"
    assert webhook_url_for_log(dirty) == "https://buyer.example.com/hook"
    assert sanitize_webhook_url_for_log(None) is None
    assert sanitize_webhook_url_for_log("not-a-url") is None
    assert webhook_url_for_log(None) == UNPARSEABLE_WEBHOOK_URL_FOR_LOG
    assert webhook_url_for_log("not-a-url") == UNPARSEABLE_WEBHOOK_URL_FOR_LOG


def test_reject_unsafe_webhook_registration_url_allows_public() -> None:
    # Registration skips DNS — fixture hostnames must not NXDOMAIN-fail.
    reject_unsafe_webhook_registration_url("https://buyer.example.com/hook", field="push_notification_config.url")


def test_reject_unsafe_webhook_registration_url_allows_unresolvable_public_hostname() -> None:
    """Registration gate must not require DNS (BDD fixture hosts)."""
    reject_unsafe_webhook_registration_url(
        "https://nonexistent-buyer-ssrf-fixture.invalid/hook",
        field="reporting_webhook.url",
    )


def test_push_notification_config_repo_upsert_rejects_ssrf_url() -> None:
    """Repository upsert is a second registration gate (A2A set_push_notification_config)."""
    from src.core.database.repositories.push_notification_config import PushNotificationConfigRepository

    repo = PushNotificationConfigRepository(MagicMock(), "t1")
    with pytest.raises(ValueError, match="Invalid webhook URL"):
        repo.upsert(
            config_id="pnc_bad",
            principal_id="p1",
            url=_METADATA_URL,
            authentication_type=None,
            authentication_token=None,
            validation_token=None,
        )


@pytest.mark.asyncio
async def test_create_media_buy_rejects_reporting_webhook_anyurl() -> None:
    """Registration gate must run for real ReportingWebhook.url (AnyUrl, not str)."""
    req = _minimal_create_request(reporting_webhook=_reporting_webhook(_METADATA_URL))
    with pytest.raises(AdCPValidationError) as exc_info:
        await _create_media_buy_impl(req, identity=_identity())
    assert exc_info.value.field == "reporting_webhook.url"
    assert "Invalid reporting_webhook.url" in exc_info.value.message


@pytest.mark.asyncio
async def test_create_media_buy_rejects_push_config_before_workflow() -> None:
    """PNC SSRF must run before workflow metadata write (wiring + ordering)."""
    req = _minimal_create_request()
    mock_ctx = MagicMock()
    with (
        patch("src.core.tools.media_buy_create.get_context_manager", return_value=mock_ctx),
        pytest.raises(AdCPValidationError) as exc_info,
    ):
        await _create_media_buy_impl(
            req,
            push_notification_config={"url": _METADATA_URL},
            identity=_identity(),
        )
    assert exc_info.value.field == "push_notification_config.url"
    mock_ctx.create_workflow_step.assert_not_called()
    mock_ctx.create_context.assert_not_called()


def test_sync_creatives_rejects_unsafe_push_config_url() -> None:
    """sync_creatives must reject metadata URL at registration before DB work."""
    with pytest.raises(AdCPValidationError) as exc_info:
        _sync_creatives_impl(
            creatives=[],
            push_notification_config={"url": _METADATA_URL},
            identity=_identity(),
        )
    assert exc_info.value.field == "push_notification_config.url"


def test_reject_unsafe_a2a_webhook_url_rejects_metadata() -> None:
    """A2A registration helper maps SSRF to InvalidParamsError + AdCP envelope in data."""
    with pytest.raises(InvalidParamsError, match="Invalid push_notification_config.url") as exc_info:
        _reject_unsafe_a2a_webhook_url(_METADATA_URL)
    assert_envelope_shape(exc_info.value.data, "VALIDATION_ERROR", recovery="correctable")
    assert exc_info.value.data["errors"][0].get("suggestion")


@pytest.mark.asyncio
async def test_a2a_message_send_rejects_unsafe_push_config_url() -> None:
    """message/send must reject metadata URL before stash."""
    handler = AdCPRequestHandler()
    text_part = Part()
    text_part.text = "list products"
    message = Message(message_id="m-ssrf", role=Role.ROLE_USER, parts=[text_part])
    push = TaskPushNotificationConfig(url=_METADATA_URL)
    params = SendMessageRequest(
        message=message,
        configuration=SendMessageConfiguration(task_push_notification_config=push),
    )

    with pytest.raises(InvalidParamsError, match="Invalid push_notification_config.url") as exc_info:
        await handler.on_message_send(params, context=MagicMock())

    assert_envelope_shape(exc_info.value.data, "VALIDATION_ERROR", recovery="correctable")
    assert handler._task_push_configs == {}


@pytest.mark.asyncio
async def test_a2a_set_push_handler_rejects_metadata_url() -> None:
    """Handler on_create_task_push_notification_config must reject before upsert."""
    handler = AdCPRequestHandler()
    identity = _identity()
    tool_context = MagicMock()
    tool_context.tenant_id = identity.tenant_id
    tool_context.principal_id = identity.principal_id
    params = TaskPushNotificationConfig(url=_METADATA_URL, task_id="task-1", id="pnc-1")

    with (
        patch.object(handler, "_get_auth_token", return_value="tok"),
        patch.object(handler, "_resolve_a2a_identity", return_value=identity),
        patch.object(handler, "_make_tool_context", return_value=tool_context),
        patch("src.a2a_server.adcp_a2a_server.PushNotificationConfigUoW") as mock_uow,
        pytest.raises(InvalidParamsError, match="Invalid push_notification_config.url") as exc_info,
    ):
        await handler.on_create_task_push_notification_config(params, context=MagicMock())

    assert_envelope_shape(exc_info.value.data, "VALIDATION_ERROR", recovery="correctable")
    mock_uow.assert_not_called()
