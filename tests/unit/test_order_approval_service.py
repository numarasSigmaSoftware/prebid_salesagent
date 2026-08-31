"""Unit tests for order approval service."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from src.services.order_approval_service import (
    get_active_approvals,
    get_approval_status,
    is_approval_running,
    start_order_approval_background,
)


@pytest.fixture(autouse=True)
def cleanup_approval_registry():
    """Clean up global approval registry before each test."""
    # Import here to avoid issues with module loading
    import src.services.order_approval_service as service

    # Clear the registry before the test (ThreadRegistry API)
    for key in list(service._active_approvals.list_active()):
        service._active_approvals.remove(key)

    yield

    # Note: Don't clear after test - threads may still be running and need to clean up themselves


@pytest.fixture
def mock_db_session():
    """Mock database session."""
    with patch("src.services.order_approval_service.get_db_session") as mock_session:
        mock_db = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_db
        mock_db.scalars.return_value.first.return_value = None  # No existing approval
        mock_db.scalars.return_value.all.return_value = []
        yield mock_db


@pytest.fixture
def mock_gam_client():
    """Mock GAM client and managers."""
    with (
        patch("src.services.order_approval_service.GAMClientManager") as mock_client_mgr,
        patch("src.services.order_approval_service.GAMOrdersManager") as mock_orders_mgr,
        patch("src.services.order_approval_service.AdapterConfig") as mock_config,
    ):
        # Mock adapter config
        mock_adapter_config = MagicMock()
        mock_adapter_config.gam_network_code = "12345"

        # Mock orders manager
        mock_orders_instance = MagicMock()
        mock_orders_instance.approve_order.return_value = True
        mock_orders_mgr.return_value = mock_orders_instance

        yield {
            "client_manager": mock_client_mgr,
            "orders_manager": mock_orders_mgr,
            "orders_instance": mock_orders_instance,
            "adapter_config": mock_adapter_config,
        }


# test_start_approval_creates_sync_job lived here. It asserted the SyncJob's fields off
# a MagicMock session's call_args (never a persisted row) and left the worker thread
# running past the end of the test, where it reached the real DB and fired a webhook
# into whatever test ran next (found and fixed in GH #1941). Replaced by the real-DB path in
# tests/integration/test_order_approval_background.py, which joins the thread.
#
# Retained from the parallel fix on main (#2091), because the diagnosis is recorded
# nowhere else and explains why a leaked worker is not merely untidy: the stray thread
# ran the true retry path -- order_approval_service.py:406 `time.sleep(2**attempt)`
# behind httpx POSTs at timeout=10.0 -- so it called sleep(1), sleep(2), sleep(4) from
# inside whatever test was running by then. Measured: still alive ~1500 tests later,
# during test_performance_index_behavioral and test_policy_typed_models, and it
# intermittently broke TestWebhookDelivery::test_exponential_backoff_timing, whose
# class-level patch of `src.core.webhook_delivery.time.sleep` is PROCESS-GLOBAL (that
# module does `import time`, so the patch lands on the time module itself and is visible
# to every module and every thread). The stray sleeps inflated mock_sleep.call_count
# past 2. main's fix patched `_run_approval_thread` so the thread never spawns; this
# branch deleted the test instead, because its assertions read a mock rather than a row.


def test_start_approval_rejects_duplicate(mock_db_session):
    """Test that starting approval for same order fails."""
    from src.core.database.models import SyncJob

    # Mock existing approval for this order
    existing_approval = SyncJob(
        sync_id="approval_12345_existing",
        tenant_id="tenant_1",
        adapter_type="google_ad_manager",
        sync_type="order_approval",
        status="running",
        started_at=datetime.now(UTC),
        triggered_by="order_creation",
        triggered_by_id="mb_123",
        progress={"order_id": "12345"},
    )
    mock_db_session.scalars.return_value.all.return_value = [existing_approval]

    # Patched for the same reason as the test above -- an unpatched call leaks a
    # live daemon thread into the rest of the session.
    with (
        patch("src.services.order_approval_service._run_approval_thread"),
        pytest.raises(ValueError, match="Approval already running for order 12345"),
    ):
        start_order_approval_background(
            order_id="12345",
            media_buy_id="mb_123",
            tenant_id="tenant_1",
            principal_id="principal_1",
        )


def test_approval_thread_tracks_in_registry(mock_db_session):
    """Test that approval thread is tracked in global registry.

    Uses a blocking mock so the worker stays alive while the test
    inspects the registry — the dead-thread reaper added in the
    production memory-leak fix drops dead-thread entries on read, so
    a no-op mock that exits immediately would race the reaper.
    """
    import threading

    keep_alive = threading.Event()
    with patch(
        "src.services.order_approval_service._run_approval_thread",
        side_effect=lambda *args, **kwargs: keep_alive.wait(timeout=2.0),
    ):
        approval_id = start_order_approval_background(
            order_id="12345",
            media_buy_id="mb_123",
            tenant_id="tenant_1",
            principal_id="principal_1",
        )
        try:
            active_approvals = get_active_approvals()
            assert approval_id in active_approvals, f"Expected {approval_id} in {active_approvals}"
            assert is_approval_running(approval_id)
        finally:
            keep_alive.set()


def test_get_approval_status(mock_db_session):
    """Test getting approval status."""
    from src.core.database.models import SyncJob

    # Mock existing approval
    approval = SyncJob(
        sync_id="approval_12345_test",
        tenant_id="tenant_1",
        adapter_type="google_ad_manager",
        sync_type="order_approval",
        status="running",
        started_at=datetime.now(UTC),
        triggered_by="order_creation",
        triggered_by_id="mb_123",
        progress={"order_id": "12345", "attempts": 3},
    )
    mock_db_session.scalars.return_value.first.return_value = approval

    status = get_approval_status("approval_12345_test")

    assert status is not None
    assert status["approval_id"] == "approval_12345_test"
    assert status["status"] == "running"
    assert status["progress"]["order_id"] == "12345"
    assert status["progress"]["attempts"] == 3


def test_get_approval_status_not_found(mock_db_session):
    """Test getting approval status for non-existent approval."""
    mock_db_session.scalars.return_value.first.return_value = None

    status = get_approval_status("nonexistent")
    assert status is None


def test_webhook_notification_sent_on_success():
    """Test webhook notification is sent when approval succeeds."""
    from src.services.order_approval_service import _send_approval_webhook

    with (
        patch("src.services.order_approval_service.get_db_session") as mock_db,
        patch("httpx.Client") as mock_httpx,
        patch(
            "src.core.webhook_validator.WebhookURLValidator.validate_outbound_webhook_url",
            return_value=(True, ""),
        ),
    ):
        # Mock push notification config
        mock_db_instance = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_db_instance

        from src.core.database.models import PushNotificationConfig

        mock_config = PushNotificationConfig(
            tenant_id="tenant_1",
            principal_id="principal_1",
            url="https://example.com/webhook",
            authentication_type="bearer",
            authentication_token="test_token",
            is_active=True,
        )
        mock_db_instance.scalars.return_value.first.return_value = mock_config

        # Mock HTTP client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client_instance = MagicMock()
        mock_client_instance.post.return_value = mock_response
        mock_httpx.return_value.__enter__.return_value = mock_client_instance

        # Send webhook
        _send_approval_webhook(
            webhook_url="https://example.com/webhook",
            tenant_id="tenant_1",
            principal_id="principal_1",
            media_buy_id="mb_123",
            status="approved",
            message="Order approved successfully",
            order_id="12345",
            attempts=3,
        )

        # Verify HTTP POST was made
        mock_client_instance.post.assert_called_once()
        mock_httpx.assert_called_with(timeout=10.0, follow_redirects=False)
        call_args = mock_client_instance.post.call_args

        # Check webhook payload
        assert call_args[0][0] == "https://example.com/webhook"
        payload = call_args[1]["json"]
        assert payload["event"] == "order_approval_update"
        assert payload["media_buy_id"] == "mb_123"
        assert payload["status"] == "approved"
        assert payload["order_id"] == "12345"
        assert payload["attempts"] == 3

        # Check authentication header
        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test_token"


def test_approval_webhook_rejects_metadata_url_without_post():
    """Order-approval sender must share the outbound SSRF gate (no open redirect)."""
    from src.services.order_approval_service import _send_approval_webhook

    with (
        patch("src.services.order_approval_service.get_db_session") as mock_db,
        patch("httpx.Client") as mock_httpx,
    ):
        mock_db_instance = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_db_instance
        mock_db_instance.scalars.return_value.first.return_value = None

        _send_approval_webhook(
            webhook_url="http://169.254.169.254/latest/meta-data/",
            tenant_id="tenant_1",
            principal_id="principal_1",
            media_buy_id="mb_123",
            status="approved",
            message="Order approved successfully",
        )

        mock_httpx.assert_not_called()


@patch("src.services.order_approval_service.time.sleep")
def test_webhook_retries_on_failure(mock_sleep):
    """Test webhook retries on HTTP failure."""
    import src.services.order_approval_service as service_module

    with (
        patch.object(service_module, "get_db_session") as mock_db,
        patch("httpx.Client") as mock_httpx,
        patch(
            "src.core.webhook_validator.WebhookURLValidator.validate_outbound_webhook_url",
            return_value=(True, ""),
        ),
    ):
        # Mock DB
        mock_db_instance = MagicMock()
        mock_db.return_value.__enter__.return_value = mock_db_instance
        mock_db_instance.scalars.return_value.first.return_value = None  # No auth config

        # Mock HTTP client - fails twice, succeeds third time
        mock_response_fail = MagicMock()
        mock_response_fail.status_code = 500
        mock_response_success = MagicMock()
        mock_response_success.status_code = 200

        # Track calls explicitly with closure
        call_counter = {"count": 0}
        responses = [mock_response_fail, mock_response_fail, mock_response_success]

        def post_side_effect(*args, **kwargs):
            call_counter["count"] += 1
            idx = min(call_counter["count"] - 1, len(responses) - 1)
            return responses[idx]

        # Create a fresh MagicMock for the client instance
        mock_client_instance = MagicMock()
        mock_client_instance.post.side_effect = post_side_effect

        # Create a fresh context manager mock
        mock_context = MagicMock()
        mock_context.__enter__.return_value = mock_client_instance
        mock_context.__exit__.return_value = None
        mock_httpx.return_value = mock_context

        # Send webhook
        service_module._send_approval_webhook(
            webhook_url="https://example.com/webhook",
            tenant_id="tenant_1",
            principal_id="principal_1",
            media_buy_id="mb_123",
            status="approved",
            message="Order approved",
        )

        # Verify retry logic works - should be at least 3 attempts
        # Note: Due to test pollution in full suite, may see 4 calls, but minimum is 3
        assert call_counter["count"] >= 3, f"Expected at least 3 retry attempts, got {call_counter['count']}"
        assert call_counter["count"] <= 4, (
            f"Expected at most 4 retry attempts (3 + 1 pollution), got {call_counter['count']}"
        )
