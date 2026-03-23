"""Tests for task management MCP tools (list_tasks, get_task, complete_task).

These tests verify that the task management tools work correctly.
Issue #816 revealed that list_tasks was broken but had no test coverage.
"""

from datetime import UTC, datetime
from unittest.mock import ANY, MagicMock, Mock, patch

import pytest

from src.core.database.models import WorkflowStep
from src.core.resolved_identity import ResolvedIdentity


class TestListTasksTool:
    """Test the list_tasks MCP tool actually works."""

    @pytest.fixture
    def mock_workflow_repo(self):
        """Create a mock WorkflowRepository."""
        repo = MagicMock()
        return repo

    @pytest.fixture
    def mock_uow(self, mock_workflow_repo):
        """Create a mock WorkflowUoW context manager."""
        uow = MagicMock()
        uow.__enter__ = Mock(return_value=uow)
        uow.__exit__ = Mock(return_value=None)
        uow.workflows = mock_workflow_repo
        return uow

    @pytest.fixture
    def sample_tenant(self):
        return {"tenant_id": "test_tenant", "name": "Test Tenant"}

    @pytest.fixture
    def sample_workflow_step(self):
        """Create a sample workflow step for testing."""
        step = Mock(spec=WorkflowStep)
        step.step_id = "step_123"
        step.context_id = "ctx_123"
        step.status = "requires_approval"
        step.step_type = "approval"
        step.tool_name = "create_media_buy"
        step.owner = "publisher"
        step.created_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        step.request_data = {"budget": 5000}
        step.response_data = None
        step.error_message = None
        step.comments = []
        return step

    async def _get_list_tasks_fn(self):
        """Get the list_tasks function from MCP tool registry."""
        from src.core.main import mcp

        tool = await mcp.get_tool("list_tasks")
        assert tool is not None, "list_tasks should be registered (unified mode is default)"
        return tool.fn

    def _make_identity(self, sample_tenant):
        """Create a ResolvedIdentity for testing."""
        return ResolvedIdentity(
            principal_id="principal_123",
            tenant_id=sample_tenant["tenant_id"],
            tenant=sample_tenant,
            protocol="mcp",
        )

    async def test_list_tasks_returns_tasks(self, mock_uow, mock_workflow_repo, sample_tenant, sample_workflow_step):
        """Test that list_tasks returns workflow steps correctly."""
        list_tasks_fn = await self._get_list_tasks_fn()

        mock_workflow_repo.count_by_tenant.return_value = 1
        mock_workflow_repo.list_by_tenant.return_value = [sample_workflow_step]
        mock_workflow_repo.get_mappings_for_steps.return_value = {"step_123": []}

        identity = self._make_identity(sample_tenant)

        with patch("src.core.tools.task_management.WorkflowUoW", return_value=mock_uow):
            result = await list_tasks_fn(identity=identity)

        assert "tasks" in result
        assert "total" in result
        assert result["total"] == 1

    async def test_list_tasks_filters_by_status(
        self, mock_uow, mock_workflow_repo, sample_tenant, sample_workflow_step
    ):
        """Test that list_tasks applies status filter."""
        list_tasks_fn = await self._get_list_tasks_fn()

        mock_workflow_repo.count_by_tenant.return_value = 1
        mock_workflow_repo.list_by_tenant.return_value = [sample_workflow_step]
        mock_workflow_repo.get_mappings_for_steps.return_value = {"step_123": []}

        identity = self._make_identity(sample_tenant)

        with patch("src.core.tools.task_management.WorkflowUoW", return_value=mock_uow):
            result = await list_tasks_fn(status="requires_approval", identity=identity)

        assert "tasks" in result
        mock_workflow_repo.count_by_tenant.assert_called_once_with(
            status="requires_approval",
            object_type=None,
            object_id=None,
        )


class TestGetTaskTool:
    """Test the get_task MCP tool actually works."""

    @pytest.fixture
    def mock_workflow_repo(self):
        repo = MagicMock()
        return repo

    @pytest.fixture
    def mock_uow(self, mock_workflow_repo):
        uow = MagicMock()
        uow.__enter__ = Mock(return_value=uow)
        uow.__exit__ = Mock(return_value=None)
        uow.workflows = mock_workflow_repo
        return uow

    @pytest.fixture
    def sample_tenant(self):
        return {"tenant_id": "test_tenant", "name": "Test Tenant"}

    @pytest.fixture
    def sample_workflow_step(self):
        step = Mock(spec=WorkflowStep)
        step.step_id = "step_123"
        step.context_id = "ctx_123"
        step.status = "requires_approval"
        step.step_type = "approval"
        step.tool_name = "create_media_buy"
        step.owner = "publisher"
        step.created_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        step.request_data = {"budget": 5000}
        step.response_data = None
        step.error_message = None
        step.comments = []
        step.transaction_details = None
        return step

    async def _get_get_task_fn(self):
        """Get the get_task function from MCP tool registry."""
        from src.core.main import mcp

        tool = await mcp.get_tool("get_task")
        assert tool is not None, "get_task should be registered (unified mode is default)"
        return tool.fn

    def _make_identity(self, sample_tenant):
        """Create a ResolvedIdentity for testing."""
        return ResolvedIdentity(
            principal_id="principal_123",
            tenant_id=sample_tenant["tenant_id"],
            tenant=sample_tenant,
            protocol="mcp",
        )

    async def test_get_task_returns_task_details(
        self, mock_uow, mock_workflow_repo, sample_tenant, sample_workflow_step
    ):
        """Test that get_task returns task details correctly."""
        get_task_fn = await self._get_get_task_fn()

        mock_workflow_repo.get_by_step_id.return_value = sample_workflow_step
        mock_workflow_repo.get_mappings_for_step.return_value = []

        identity = self._make_identity(sample_tenant)

        with patch("src.core.tools.task_management.WorkflowUoW", return_value=mock_uow):
            result = await get_task_fn(task_id="step_123", identity=identity)

        assert result["task_id"] == "step_123"
        assert result["status"] == "requires_approval"

    async def test_get_task_not_found_raises_error(self, mock_uow, mock_workflow_repo, sample_tenant):
        """Test that get_task raises ToolError when task not found.

        The MCP boundary (with_error_logging) translates ValueError to
        ToolError with VALIDATION_ERROR code. This is correct: business
        logic raises ValueError, the transport boundary translates it.
        """
        from fastmcp.exceptions import ToolError

        get_task_fn = await self._get_get_task_fn()

        mock_workflow_repo.get_by_step_id.return_value = None

        identity = self._make_identity(sample_tenant)

        with patch("src.core.tools.task_management.WorkflowUoW", return_value=mock_uow):
            with pytest.raises(ToolError, match="not found"):
                await get_task_fn(task_id="nonexistent", identity=identity)


class TestCompleteTaskTool:
    """Test the complete_task MCP tool actually works."""

    @pytest.fixture
    def mock_workflow_repo(self):
        repo = MagicMock()
        return repo

    @pytest.fixture
    def mock_uow(self, mock_workflow_repo):
        uow = MagicMock()
        uow.__enter__ = Mock(return_value=uow)
        uow.__exit__ = Mock(return_value=None)
        uow.workflows = mock_workflow_repo
        return uow

    @pytest.fixture
    def sample_tenant(self):
        return {"tenant_id": "test_tenant", "name": "Test Tenant"}

    @pytest.fixture
    def sample_pending_step(self):
        step = Mock(spec=WorkflowStep)
        step.step_id = "step_123"
        step.context_id = "ctx_123"
        step.status = "requires_approval"
        step.step_type = "approval"
        step.tool_name = "create_media_buy"
        step.owner = "publisher"
        step.created_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        step.completed_at = None
        step.request_data = {"budget": 5000}
        step.response_data = None
        step.error_message = None
        step.comments = []
        return step

    async def _get_complete_task_fn(self):
        """Get the complete_task function from MCP tool registry."""
        from src.core.main import mcp

        tool = await mcp.get_tool("complete_task")
        assert tool is not None, "complete_task should be registered (unified mode is default)"
        return tool.fn

    def _make_identity(self, sample_tenant):
        """Create a ResolvedIdentity for testing."""
        return ResolvedIdentity(
            principal_id="principal_123",
            tenant_id=sample_tenant["tenant_id"],
            tenant=sample_tenant,
            protocol="mcp",
        )

    async def test_complete_task_updates_status(self, mock_uow, mock_workflow_repo, sample_tenant, sample_pending_step):
        """Test that complete_task updates task status."""
        complete_task_fn = await self._get_complete_task_fn()

        mock_workflow_repo.get_by_step_id.return_value = sample_pending_step
        mock_workflow_repo.update_status.return_value = sample_pending_step

        identity = self._make_identity(sample_tenant)

        with patch("src.core.tools.task_management.AdminCreativeUoW", return_value=mock_uow):
            result = await complete_task_fn(task_id="step_123", status="completed", identity=identity)

        assert result["status"] == "completed"
        assert result["task_id"] == "step_123"
        mock_workflow_repo.update_status.assert_called_once_with(
            "step_123",
            status="completed",
            completed_at=ANY,
            response_data={"manually_completed": True, "completed_by": "principal_123"},
        )

    async def test_complete_task_rejects_invalid_status(self, mock_uow, mock_workflow_repo, sample_tenant):
        """Test that complete_task rejects invalid status values.

        The MCP boundary (with_error_logging) translates ValueError to
        ToolError with VALIDATION_ERROR code.
        """
        from fastmcp.exceptions import ToolError

        complete_task_fn = await self._get_complete_task_fn()

        identity = self._make_identity(sample_tenant)

        with pytest.raises(ToolError, match="Invalid status"):
            await complete_task_fn(task_id="step_123", status="invalid_status", identity=identity)

    async def test_complete_task_creative_approval_approved_runs_shared_review_flow(
        self, mock_uow, mock_workflow_repo, sample_tenant
    ):
        """Creative approval completion must use the shared review flow and side effects."""
        complete_task_fn = await self._get_complete_task_fn()

        approval_step = Mock(spec=WorkflowStep)
        approval_step.step_id = "step_creative_123"
        approval_step.context_id = "ctx_123"
        approval_step.status = "requires_approval"
        approval_step.step_type = "creative_approval"
        approval_step.tool_name = "sync_creatives"
        approval_step.owner = "publisher"
        approval_step.created_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        approval_step.completed_at = None
        approval_step.request_data = {}
        approval_step.response_data = None
        approval_step.error_message = None
        approval_step.comments = []

        mock_mapping = Mock()
        mock_mapping.object_type = "creative"
        mock_mapping.object_id = "creative_123"

        mock_workflow_repo.get_by_step_id.return_value = approval_step
        mock_workflow_repo.get_mappings_for_step.return_value = [mock_mapping]

        identity = self._make_identity(sample_tenant)
        side_effect = Mock()

        with (
            patch("src.core.tools.task_management.AdminCreativeUoW", return_value=mock_uow),
            patch("src.core.tools.task_management.apply_creative_review_decision", return_value=side_effect) as review,
            patch("src.core.tools.task_management.execute_creative_review_side_effects") as execute_side_effects,
        ):
            result = await complete_task_fn(
                task_id="step_creative_123",
                status="completed",
                response_data={"decision": "approved", "reviewer": "qa@example.com"},
                identity=identity,
            )

        assert result["status"] == "completed"
        review.assert_called_once_with(
            mock_uow,
            creative_id="creative_123",
            decision="approved",
            actor="principal_123",
            rejection_reason=None,
        )
        execute_side_effects.assert_called_once_with(
            [side_effect],
            tenant_id="test_tenant",
            actor="principal_123",
            operation="complete_task",
        )

    async def test_complete_task_creative_approval_rejected_prefers_rejection_reason(
        self, mock_uow, mock_workflow_repo, sample_tenant
    ):
        """Creative rejection completion must persist the normalized rejection reason."""
        complete_task_fn = await self._get_complete_task_fn()

        rejection_step = Mock(spec=WorkflowStep)
        rejection_step.step_id = "step_creative_456"
        rejection_step.context_id = "ctx_456"
        rejection_step.status = "requires_approval"
        rejection_step.step_type = "creative_approval"
        rejection_step.tool_name = "sync_creatives"
        rejection_step.owner = "publisher"
        rejection_step.created_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        rejection_step.completed_at = None
        rejection_step.request_data = {}
        rejection_step.response_data = None
        rejection_step.error_message = None
        rejection_step.comments = []

        mock_mapping = Mock()
        mock_mapping.object_type = "creative"
        mock_mapping.object_id = "creative_456"

        mock_workflow_repo.get_by_step_id.return_value = rejection_step
        mock_workflow_repo.get_mappings_for_step.return_value = [mock_mapping]

        identity = self._make_identity(sample_tenant)

        with (
            patch("src.core.tools.task_management.AdminCreativeUoW", return_value=mock_uow),
            patch("src.core.tools.task_management.apply_creative_review_decision", return_value=Mock()) as review,
            patch("src.core.tools.task_management.execute_creative_review_side_effects"),
        ):
            await complete_task_fn(
                task_id="step_creative_456",
                status="completed",
                response_data={
                    "decision": "rejected",
                    "reason": "legacy value",
                    "rejection_reason": "Policy violation",
                },
                identity=identity,
            )

        review.assert_called_once_with(
            mock_uow,
            creative_id="creative_456",
            decision="rejected",
            actor="principal_123",
            rejection_reason="Policy violation",
        )
