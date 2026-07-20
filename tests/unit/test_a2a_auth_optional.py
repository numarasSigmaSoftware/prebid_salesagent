#!/usr/bin/env python3
"""
Unit tests for A2A auth-optional discovery endpoints.

Tests that discovery endpoints (list_creative_formats, list_authorized_properties, get_products)
properly handle both authenticated and unauthenticated requests according to AdCP spec.

After the identity-at-transport-boundary refactor (salesagent-anjp), handlers receive
a pre-resolved identity parameter rather than resolving auth internally.
"""

from unittest.mock import AsyncMock, patch

import pytest
from a2a.types import InvalidRequestError

from src.a2a_server.adcp_a2a_server import AdCPRequestHandler
from src.core.exceptions import AdCPValidationError
from tests.factories.principal import PrincipalFactory


class TestAuthOptionalSkills:
    """Test auth-optional skill handling in A2A server."""

    def setup_method(self):
        """Set up test fixtures."""
        self.handler = AdCPRequestHandler()
        self.mock_identity = PrincipalFactory.make_identity(
            principal_id="test_principal", tenant_id="default", tenant={"tenant_id": "default"}, protocol="a2a"
        )
        self.anon_identity = PrincipalFactory.make_identity(
            principal_id=None, tenant_id="default", tenant={"tenant_id": "default"}, protocol="a2a"
        )

    @pytest.mark.asyncio
    async def test_list_creative_formats_without_auth(self):
        """list_creative_formats should work with anonymous identity (no principal)."""
        with patch("src.a2a_server.adcp_a2a_server.core_list_creative_formats_tool") as mock_tool:
            mock_tool.return_value = {"formats": []}

            result = await self.handler._handle_list_creative_formats_skill(parameters={}, identity=self.anon_identity)

            assert result is not None
            assert "formats" in result
            mock_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_creative_formats_with_auth(self):
        """list_creative_formats should work with authenticated identity."""
        with patch("src.a2a_server.adcp_a2a_server.core_list_creative_formats_tool") as mock_tool:
            mock_tool.return_value = {"formats": []}

            result = await self.handler._handle_list_creative_formats_skill(parameters={}, identity=self.mock_identity)

            assert result is not None
            mock_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_authorized_properties_without_auth(self):
        """list_authorized_properties should work with anonymous identity."""
        with patch("src.a2a_server.adcp_a2a_server.core_list_authorized_properties_tool") as mock_tool:
            mock_tool.return_value = {"publisher_domains": []}

            result = await self.handler._handle_list_authorized_properties_skill(
                parameters={}, identity=self.anon_identity
            )

            assert result is not None
            mock_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_authorized_properties_with_auth(self):
        """list_authorized_properties should work with authenticated identity."""
        with patch("src.a2a_server.adcp_a2a_server.core_list_authorized_properties_tool") as mock_tool:
            mock_tool.return_value = {"publisher_domains": []}

            result = await self.handler._handle_list_authorized_properties_skill(
                parameters={}, identity=self.mock_identity
            )

            assert result is not None
            mock_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_products_without_auth(self):
        """get_products should work with anonymous identity."""
        with patch("src.a2a_server.adcp_a2a_server.core_get_products_tool") as mock_tool:
            mock_tool.return_value = {"products": []}

            result = await self.handler._handle_get_products_skill(
                parameters={"brief": "test campaign"}, identity=self.anon_identity
            )

            assert result is not None
            mock_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_products_with_auth(self):
        """get_products should work with authenticated identity."""
        with patch("src.a2a_server.adcp_a2a_server.core_get_products_tool") as mock_tool:
            mock_tool.return_value = {"products": []}

            result = await self.handler._handle_get_products_skill(
                parameters={"brief": "test campaign"}, identity=self.mock_identity
            )

            assert result is not None
            mock_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_media_buy_requires_auth(self):
        """create_media_buy should reject None identity (not a discovery endpoint)."""
        with pytest.raises(InvalidRequestError) as exc_info:
            await self.handler._handle_explicit_skill(
                skill_name="create_media_buy", parameters={"product_ids": ["prod_1"]}, identity=None
            )

        assert "Authentication required" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_update_media_buy_requires_auth(self):
        """update_media_buy should reject None identity."""
        with pytest.raises(InvalidRequestError) as exc_info:
            await self.handler._handle_explicit_skill(
                skill_name="update_media_buy", parameters={"media_buy_id": "mb_1"}, identity=None
            )

        assert "Authentication required" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_anonymous_discovery_callback_is_refused_and_never_persisted(self):
        """Auth-optional discovery carrying a push callback must not register it (SSRF, #1512).

        An auth-optional discovery request that resolves an anonymous identity
        SUCCESSFULLY and carries a callback: the callback must be refused before
        storage, so the later status/failure webhook has no attacker-chosen
        target (e.g. http://169.254.169.254/latest/meta-data) to POST to.

        The rejection surfaces as a buyer-CORRECTABLE FAILED Task (not a JSON-RPC
        InternalError) — the wire-envelope shape is pinned by the raw-wire test in
        tests/integration/test_a2a_error_responses.py (#1512).
        """
        from a2a.server.routes.common import ServerCallContext
        from a2a.types import (
            SendMessageConfiguration,
            SendMessageRequest,
            Task,
            TaskPushNotificationConfig,
            TaskState,
        )

        from tests.utils.a2a_helpers import create_a2a_message_with_skill

        params = SendMessageRequest(
            message=create_a2a_message_with_skill("get_adcp_capabilities", {}),
            configuration=SendMessageConfiguration(
                task_push_notification_config=TaskPushNotificationConfig(url="http://169.254.169.254/latest/meta-data")
            ),
        )

        with patch.object(self.handler, "_resolve_a2a_identity", return_value=self.anon_identity):
            result = await self.handler.on_message_send(params, ServerCallContext())

        # Buyer-correctable FAILED task, NOT an InternalError; callback never stored so
        # the status/failure webhook has nothing to deliver.
        assert isinstance(result, Task)
        assert result.status.state == TaskState.TASK_STATE_FAILED
        assert self.handler._task_push_configs == {}

    def test_validate_push_callback_rejects_ssrf_url_for_authenticated_caller(self):
        """Even an authenticated caller cannot register an internal/metadata callback URL (#1512)."""
        from a2a.types import TaskPushNotificationConfig

        config = TaskPushNotificationConfig(url="http://169.254.169.254/latest/meta-data")
        with pytest.raises(AdCPValidationError) as exc:
            self.handler._validate_push_callback(config, self.mock_identity)
        assert "SSRF" in str(exc.value)

    def test_validate_push_callback_rejects_anonymous_caller_even_for_safe_url(self):
        """The auth gate refuses an anonymous caller's callback INDEPENDENT of the SSRF check.

        Uses a benign public URL and forces the SSRF validator to pass, so the ONLY
        thing that can reject is the authentication gate — isolating it from the SSRF
        branch (the metadata-URL test would pass even if the auth gate regressed).
        """
        from a2a.types import TaskPushNotificationConfig

        config = TaskPushNotificationConfig(url="https://buyer.example.com/webhook")
        with (
            patch(
                "src.a2a_server.adcp_a2a_server.WebhookURLValidator.validate_callback_url",
                return_value=(True, ""),
            ),
            pytest.raises(AdCPValidationError) as exc,
        ):
            self.handler._validate_push_callback(config, self.anon_identity)
        assert "authentication" in str(exc.value).lower()

    def test_validate_push_callback_allows_safe_url_for_authenticated_caller(self):
        """A safe callback URL from an authenticated caller is accepted (no over-rejection)."""
        from a2a.types import TaskPushNotificationConfig

        config = TaskPushNotificationConfig(url="https://buyer.example.com/webhook")
        with patch(
            "src.a2a_server.adcp_a2a_server.WebhookURLValidator.validate_callback_url",
            return_value=(True, ""),
        ):
            self.handler._validate_push_callback(config, self.mock_identity)  # must not raise

    @pytest.mark.asyncio
    async def test_send_protocol_webhook_skips_ssrf_url_at_delivery(self):
        """Delivery re-validates the callback URL and skips an SSRF target (DNS-rebinding/TOCTOU, #1512)."""
        from a2a.types import Task, TaskPushNotificationConfig, TaskState, TaskStatus

        task = Task(id="task_ssrf", context_id="ctx", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
        # Simulate a callback that reached storage (e.g. via a non-on_message_send path,
        # or a hostname that only now resolves to a link-local address).
        self.handler._task_push_configs["task_ssrf"] = TaskPushNotificationConfig(
            url="http://169.254.169.254/latest/meta-data"
        )

        with patch("src.a2a_server.adcp_a2a_server.get_protocol_webhook_service") as mock_service:
            mock_service.return_value.send_notification = AsyncMock()
            await self.handler._send_protocol_webhook(task, status="completed")
            mock_service.return_value.send_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_discovery_skills_accept_anonymous_identity(self):
        """Discovery skills should accept anonymous identity (no principal_id)."""
        discovery_skills = {
            "list_creative_formats": "src.a2a_server.adcp_a2a_server.core_list_creative_formats_tool",
            "list_authorized_properties": "src.a2a_server.adcp_a2a_server.core_list_authorized_properties_tool",
            "get_products": "src.a2a_server.adcp_a2a_server.core_get_products_tool",
        }

        for skill_name, mock_path in discovery_skills.items():
            with patch(mock_path) as mock_tool:
                mock_tool.return_value = {}
                try:
                    await self.handler._handle_explicit_skill(
                        skill_name=skill_name,
                        parameters={"brief": "test"} if skill_name == "get_products" else {},
                        identity=self.anon_identity,
                    )
                except InvalidRequestError as e:
                    assert "Authentication required" not in str(e)

    @pytest.mark.asyncio
    async def test_natural_language_without_auth(self):
        """Natural language requests (empty skill_invocations) should not require auth.

        With the identity-at-transport-boundary refactor, on_message_send resolves
        identity at the transport boundary. NL requests with no auth get
        requires_auth=False, so identity resolution succeeds with anonymous identity.
        """
        # Build a real protobuf SendMessageRequest with NL text
        from a2a.server.routes.common import ServerCallContext
        from a2a.types import Message, Part, Role, SendMessageRequest

        message = Message(
            message_id="test_msg_1",
            context_id="test_ctx_1",
            role=Role.ROLE_USER,
        )
        message.parts.append(Part(text="show me available products"))
        params = SendMessageRequest(message=message)

        # Mock _get_auth_token to return None (no auth)
        with patch.object(self.handler, "_get_auth_token", return_value=None):
            # Mock _resolve_a2a_identity to return anonymous identity
            with patch.object(self.handler, "_resolve_a2a_identity", return_value=self.anon_identity):
                # Mock the _get_products method that would be called for natural language
                with patch.object(self.handler, "_get_products", new_callable=AsyncMock) as mock_products:
                    mock_products.return_value = {"products": []}

                    try:
                        result = await self.handler.on_message_send(params, context=ServerCallContext())
                        assert result is not None
                    except InvalidRequestError as e:
                        if "Authentication" in str(e) or "authentication" in str(e):
                            pytest.fail(f"Natural language request without auth should not require auth: {e}")
                        else:
                            raise
