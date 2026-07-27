"""Webhook URL validation to prevent SSRF attacks.

This module provides security validation for webhook URLs to prevent
Server-Side Request Forgery (SSRF) attacks where malicious users could
trick the server into making requests to internal services.
"""

import os
from typing import Any
from urllib.parse import urlparse

from adcp.types import TaskType

from src.core.security.url_validator import check_url_ssrf

# Fallback used when an action label is not a member of the SDK's closed
# TaskType enum. create_mcp_webhook_payload() restricts task_type to that
# enum and would otherwise reject the payload as schema-invalid.
WEBHOOK_TASK_TYPE_FALLBACK = "update_media_buy"


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


class WebhookURLValidator:
    """Validates webhook URLs to prevent SSRF attacks."""

    @classmethod
    def validate_webhook_url(cls, url: str) -> tuple[bool, str]:
        """
        Validate webhook URL for SSRF protection.

        Args:
            url: The webhook URL to validate

        Returns:
            (is_valid, error_message) - is_valid is True if safe, error_message explains failures
        """
        return check_url_ssrf(url)

    @classmethod
    def validate_protocol_webhook_url(cls, url: str) -> tuple[bool, str]:
        """Validate a protocol callback, with one explicit in-network test seam.

        Production callbacks require HTTPS because notification payloads and
        legacy Bearer credentials must not cross a plaintext connection. The
        E2E Docker stack may use HTTP only for its exact runner hostname while
        in development mode so delivery can be exercised without a public
        relay. Arbitrary private hosts are never enabled by this seam.
        """
        environment = os.getenv("ENVIRONMENT", "").lower()
        is_development = environment == "development"
        is_valid, error = check_url_ssrf(url, require_https=not is_development)
        if is_valid:
            return True, ""

        test_host = os.getenv("ADCP_WEBHOOK_TEST_HOST")
        parsed = urlparse(url)
        try:
            _port = parsed.port
        except ValueError:
            return False, error
        if (
            is_development
            and test_host
            and parsed.hostname == test_host
            and parsed.scheme in {"http", "https"}
            and parsed.username is None
            and parsed.password is None
        ):
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
        is_valid, error = check_url_ssrf(url)

        # If validation failed but it's a localhost error and we allow it
        if not is_valid and allow_localhost:
            if "localhost" in error.lower() or "127.0.0" in error or "loopback" in error.lower():
                return True, ""

        return is_valid, error
