"""Integration tests for MediaBuyStatusScheduler.

These tests verify that the scheduler correctly transitions media buy statuses
based on flight dates:
- pending_activation -> active (when start_time passed and creatives approved)
- scheduled -> active (when start_time passed)
- active -> completed (when end_time passed)

Uses real PostgreSQL database via integration_db fixture.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.core.database.database_session import get_db_session
from src.core.database.models import (
    Creative,
    CreativeAssignment,
    CurrencyLimit,
    MediaBuy,
    Principal,
    PropertyTag,
    Tenant,
)
from src.services.media_buy_status_scheduler import MediaBuyStatusScheduler


def _create_test_tenant(tenant_id: str = "test_tenant") -> str:
    """Create a test tenant with required setup data."""
    with get_db_session() as session:
        tenant = Tenant(
            tenant_id=tenant_id,
            name="Test Tenant",
            subdomain="test",
            ad_server="mock",
            is_active=True,
        )
        session.add(tenant)

        # Required: CurrencyLimit
        currency_limit = CurrencyLimit(
            tenant_id=tenant_id,
            currency_code="USD",
            min_package_budget=1.00,
            max_daily_package_spend=100000.00,
        )
        session.add(currency_limit)

        # Required: PropertyTag
        property_tag = PropertyTag(
            tenant_id=tenant_id,
            tag_id="all_inventory",
            name="All Inventory",
            description="All available inventory",
        )
        session.add(property_tag)

        session.commit()

    return tenant_id


def _create_test_principal(tenant_id: str, principal_id: str = "test_principal") -> str:
    """Create a test principal."""
    with get_db_session() as session:
        principal = Principal(
            tenant_id=tenant_id,
            principal_id=principal_id,
            name="Test Principal",
            access_token="test_token",
            platform_mappings={"mock": {"advertiser_id": "mock_adv_123"}},
        )
        session.add(principal)
        session.commit()

    return principal_id


def _create_media_buy(
    tenant_id: str,
    principal_id: str,
    media_buy_id: str,
    status: str,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    start_date=None,
    end_date=None,
) -> str:
    """Create a media buy with specified status and flight dates.

    If start_date/end_date are not provided, they are derived from start_time/end_time.
    Pass explicit values to override this behavior.
    """
    # Derive start_date and end_date from start_time and end_time if not explicitly provided
    now = datetime.now(UTC)
    if start_date is None:
        start_date = start_time.date() if start_time else now.date()
    if end_date is None:
        end_date = end_time.date() if end_time else (now + timedelta(days=7)).date()

    with get_db_session() as session:
        media_buy = MediaBuy(
            tenant_id=tenant_id,
            principal_id=principal_id,
            media_buy_id=media_buy_id,
            status=status,
            start_date=start_date,
            end_date=end_date,
            start_time=start_time,
            end_time=end_time,
            order_name="Test Order",
            advertiser_name="Test Advertiser",
            raw_request={},  # Required field
        )
        session.add(media_buy)
        session.commit()

    return media_buy_id


def _create_creative(
    tenant_id: str,
    principal_id: str,
    creative_id: str,
    status: str = "approved",
) -> str:
    """Create a creative with specified status."""
    with get_db_session() as session:
        creative = Creative(
            tenant_id=tenant_id,
            principal_id=principal_id,
            creative_id=creative_id,
            name="Test Creative",
            agent_url="https://creative.adcontextprotocol.org",
            format="display_300x250",
            status=status,
            data={"type": "display", "width": 300, "height": 250},
        )
        session.add(creative)
        session.commit()

    return creative_id


def _create_creative_assignment(
    tenant_id: str,
    media_buy_id: str,
    creative_id: str,
    principal_id: str = "test_principal",
) -> None:
    """Assign a creative to a media buy."""
    import uuid

    with get_db_session() as session:
        assignment = CreativeAssignment(
            assignment_id=f"assign_{uuid.uuid4().hex[:8]}",
            tenant_id=tenant_id,
            principal_id=principal_id,
            media_buy_id=media_buy_id,
            creative_id=creative_id,
            package_id="default_package",  # Required field
        )
        session.add(assignment)
        session.commit()


def _get_media_buy_status(tenant_id: str, media_buy_id: str) -> str:
    """Get the current status of a media buy."""
    with get_db_session() as session:
        from sqlalchemy import select

        stmt = select(MediaBuy).filter_by(tenant_id=tenant_id, media_buy_id=media_buy_id)
        media_buy = session.scalars(stmt).first()
        return media_buy.status if media_buy else None


# =============================================================================
# Test: scheduled -> active (when start time has passed)
# =============================================================================


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_scheduled_transitions_to_active_when_start_time_passed(integration_db):
    """Media buy in 'scheduled' status should transition to 'active' when start_time passes."""
    tenant_id = _create_test_tenant("tenant_scheduled_active")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy with start_time in the past
    past_start = datetime.now(UTC) - timedelta(hours=1)
    future_end = datetime.now(UTC) + timedelta(days=7)

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_scheduled_to_active",
        status="scheduled",
        start_time=past_start,
        end_time=future_end,
    )

    # Verify initial status
    assert _get_media_buy_status(tenant_id, media_buy_id) == "scheduled"

    # Run scheduler
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify status changed to active
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_scheduled_stays_scheduled_when_start_time_not_passed(integration_db):
    """Media buy in 'scheduled' status should stay 'scheduled' if start_time is in the future."""
    tenant_id = _create_test_tenant("tenant_scheduled_stays")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy with start_time in the future
    future_start = datetime.now(UTC) + timedelta(hours=1)
    future_end = datetime.now(UTC) + timedelta(days=7)

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_scheduled_stays",
        status="scheduled",
        start_time=future_start,
        end_time=future_end,
    )

    # Verify initial status
    assert _get_media_buy_status(tenant_id, media_buy_id) == "scheduled"

    # Run scheduler
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify status unchanged
    assert _get_media_buy_status(tenant_id, media_buy_id) == "scheduled"


# =============================================================================
# Test: pending_activation -> active (when start time passed AND creatives approved)
# =============================================================================


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_pending_activation_transitions_to_active_with_approved_creatives(integration_db):
    """Media buy in 'pending_activation' should transition to 'active' when start_time passes and creatives approved."""
    tenant_id = _create_test_tenant("tenant_pending_active")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy with start_time in the past
    past_start = datetime.now(UTC) - timedelta(hours=1)
    future_end = datetime.now(UTC) + timedelta(days=7)

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_pending_to_active",
        status="pending_activation",
        start_time=past_start,
        end_time=future_end,
    )

    # Create an approved creative and assign it to the media buy
    creative_id = _create_creative(
        tenant_id=tenant_id,
        principal_id=principal_id,
        creative_id="creative_approved",
        status="approved",
    )
    _create_creative_assignment(tenant_id, media_buy_id, creative_id)

    # Verify initial status
    assert _get_media_buy_status(tenant_id, media_buy_id) == "pending_activation"

    # Run scheduler
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify status changed to active
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_pending_activation_stays_pending_with_unapproved_creatives(integration_db):
    """Media buy in 'pending_activation' should stay pending if creatives are not approved."""
    tenant_id = _create_test_tenant("tenant_pending_unapproved")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy with start_time in the past
    past_start = datetime.now(UTC) - timedelta(hours=1)
    future_end = datetime.now(UTC) + timedelta(days=7)

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_pending_unapproved",
        status="pending_activation",
        start_time=past_start,
        end_time=future_end,
    )

    # Create a pending creative and assign it
    creative_id = _create_creative(
        tenant_id=tenant_id,
        principal_id=principal_id,
        creative_id="creative_pending",
        status="pending_approval",  # Not approved!
    )
    _create_creative_assignment(tenant_id, media_buy_id, creative_id)

    # Verify initial status
    assert _get_media_buy_status(tenant_id, media_buy_id) == "pending_activation"

    # Run scheduler
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify status unchanged (creatives not approved)
    assert _get_media_buy_status(tenant_id, media_buy_id) == "pending_activation"


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_pending_activation_activates_without_creatives(integration_db):
    """Media buy in 'pending_activation' with no creatives should transition to 'active'."""
    tenant_id = _create_test_tenant("tenant_pending_no_creatives")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy with start_time in the past - NO creatives assigned
    past_start = datetime.now(UTC) - timedelta(hours=1)
    future_end = datetime.now(UTC) + timedelta(days=7)

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_pending_no_creatives",
        status="pending_activation",
        start_time=past_start,
        end_time=future_end,
    )

    # Verify initial status
    assert _get_media_buy_status(tenant_id, media_buy_id) == "pending_activation"

    # Run scheduler
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify status changed to active (no creatives = nothing to block)
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_pending_activation_stays_pending_when_start_time_not_passed(integration_db):
    """Media buy in 'pending_activation' should stay pending if start_time is in the future."""
    tenant_id = _create_test_tenant("tenant_pending_future")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy with start_time in the future
    future_start = datetime.now(UTC) + timedelta(hours=1)
    future_end = datetime.now(UTC) + timedelta(days=7)

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_pending_future",
        status="pending_activation",
        start_time=future_start,
        end_time=future_end,
    )

    # Create approved creative
    creative_id = _create_creative(
        tenant_id=tenant_id,
        principal_id=principal_id,
        creative_id="creative_approved_future",
        status="approved",
    )
    _create_creative_assignment(tenant_id, media_buy_id, creative_id)

    # Verify initial status
    assert _get_media_buy_status(tenant_id, media_buy_id) == "pending_activation"

    # Run scheduler
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify status unchanged (start time not passed)
    assert _get_media_buy_status(tenant_id, media_buy_id) == "pending_activation"


# =============================================================================
# Test: active -> completed (when end time has passed)
# =============================================================================


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_active_transitions_to_completed_when_end_time_passed(integration_db):
    """Media buy in 'active' status should transition to 'completed' when end_time passes."""
    tenant_id = _create_test_tenant("tenant_active_completed")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy with end_time in the past
    past_start = datetime.now(UTC) - timedelta(days=7)
    past_end = datetime.now(UTC) - timedelta(hours=1)

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_active_to_completed",
        status="active",
        start_time=past_start,
        end_time=past_end,
    )

    # Verify initial status
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"

    # Run scheduler
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify status changed to completed
    assert _get_media_buy_status(tenant_id, media_buy_id) == "completed"


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_active_stays_active_when_end_time_not_passed(integration_db):
    """Media buy in 'active' status should stay 'active' if end_time is in the future."""
    tenant_id = _create_test_tenant("tenant_active_stays")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy with end_time in the future
    past_start = datetime.now(UTC) - timedelta(days=1)
    future_end = datetime.now(UTC) + timedelta(days=7)

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_active_stays",
        status="active",
        start_time=past_start,
        end_time=future_end,
    )

    # Verify initial status
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"

    # Run scheduler
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify status unchanged
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"


# =============================================================================
# Test: Multiple media buys in single run
# =============================================================================


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_scheduler_updates_multiple_media_buys(integration_db):
    """Scheduler should update multiple media buys in a single run."""
    tenant_id = _create_test_tenant("tenant_multi")
    principal_id = _create_test_principal(tenant_id)

    now = datetime.now(UTC)

    # Media buy 1: scheduled -> active
    _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_multi_1",
        status="scheduled",
        start_time=now - timedelta(hours=1),
        end_time=now + timedelta(days=7),
    )

    # Media buy 2: active -> completed
    _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_multi_2",
        status="active",
        start_time=now - timedelta(days=7),
        end_time=now - timedelta(hours=1),
    )

    # Media buy 3: scheduled but start_time in future (no change)
    _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_multi_3",
        status="scheduled",
        start_time=now + timedelta(hours=1),
        end_time=now + timedelta(days=7),
    )

    # Verify initial statuses
    assert _get_media_buy_status(tenant_id, "mb_multi_1") == "scheduled"
    assert _get_media_buy_status(tenant_id, "mb_multi_2") == "active"
    assert _get_media_buy_status(tenant_id, "mb_multi_3") == "scheduled"

    # Run scheduler
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify expected transitions
    assert _get_media_buy_status(tenant_id, "mb_multi_1") == "active"
    assert _get_media_buy_status(tenant_id, "mb_multi_2") == "completed"
    assert _get_media_buy_status(tenant_id, "mb_multi_3") == "scheduled"  # No change


# =============================================================================
# Test: Edge cases
# =============================================================================


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_scheduler_uses_start_date_when_start_time_not_set(integration_db):
    """Scheduler should fall back to start_date/end_date when start_time/end_time are not set."""
    tenant_id = _create_test_tenant("tenant_date_fallback")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy with start_date in the past but no start_time
    past_date = (datetime.now(UTC) - timedelta(days=1)).date()
    future_date = (datetime.now(UTC) + timedelta(days=7)).date()

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_date_fallback",
        status="scheduled",
        start_time=None,  # No start_time
        end_time=None,  # No end_time
        start_date=past_date,  # But start_date is in the past
        end_date=future_date,
    )

    # Verify initial status
    assert _get_media_buy_status(tenant_id, media_buy_id) == "scheduled"

    # Run scheduler - should use start_date for transition
    scheduler = MediaBuyStatusScheduler()
    await scheduler._update_statuses()

    # Verify status changed to active (using start_date fallback)
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_scheduler_idempotent(integration_db):
    """Running scheduler multiple times should be idempotent."""
    tenant_id = _create_test_tenant("tenant_idempotent")
    principal_id = _create_test_principal(tenant_id)

    # Create media buy that should transition
    past_start = datetime.now(UTC) - timedelta(hours=1)
    future_end = datetime.now(UTC) + timedelta(days=7)

    media_buy_id = _create_media_buy(
        tenant_id=tenant_id,
        principal_id=principal_id,
        media_buy_id="mb_idempotent",
        status="scheduled",
        start_time=past_start,
        end_time=future_end,
    )

    scheduler = MediaBuyStatusScheduler()

    # Run scheduler first time
    await scheduler._update_statuses()
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"

    # Run scheduler second time - should be no-op
    await scheduler._update_statuses()
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"

    # Run scheduler third time - still no-op
    await scheduler._update_statuses()
    assert _get_media_buy_status(tenant_id, media_buy_id) == "active"


# =============================================================================
# Test: legacy serving aliases (ready/approved) are migrated, not stranded
# =============================================================================


@pytest.mark.requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ready", "approved"])
async def test_legacy_serving_alias_transitions_to_active_when_start_time_passed(integration_db, status):
    """A mid-flight legacy serving alias ('ready'/'approved') is migrated to 'active'.

    The scheduler's query previously omitted 'ready' entirely,
    and its activation branch hardcoded a partial status list — so even a fetched
    'ready' row was ignored mid-flight. 'ready' is a purely date-gated legacy
    serving alias (already approved), so it must activate without a creative
    check, exactly like 'scheduled'.

    'approved': maps to the same generic serving state in
    PERSISTED_STATUS_TO_CANONICAL (purely date-gated; creative-gating lives in
    pending_start/pending_activation), so the scheduler must promote it once the
    flight starts — even with no creatives assigned.
    """
    from src.core.database.models import MediaBuy as MediaBuyModel
    from tests.factories import MediaBuyFactory
    from tests.harness._base import IntegrationEnv

    with IntegrationEnv(tenant_id=f"t_1556_{status}", principal_id=f"p_1556_{status}") as env:
        tenant, principal = env.setup_default_data()
        buy = MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id=f"mb_1556_{status}_active",
            status=status,
            start_date=(datetime.now(UTC) - timedelta(hours=1)).date(),
            end_date=(datetime.now(UTC) + timedelta(days=7)).date(),
            start_time=datetime.now(UTC) - timedelta(hours=1),
            end_time=datetime.now(UTC) + timedelta(days=7),
        )

        await MediaBuyStatusScheduler()._update_statuses()

        env.get_session().expire_all()
        row = env.get_one(MediaBuyModel, media_buy_id=buy.media_buy_id)
        assert row.status == "active"


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_legacy_ready_transitions_to_completed_when_end_time_passed(integration_db):
    """A legacy 'ready' row past its flight end is migrated to 'completed'.

    Pins the deliberate behavior change: post-flight legacy serving
    aliases auto-complete, catching the persisted column up with what the read
    tools already report (resolve_canonical_status date-refines them to
    'completed').
    """
    from src.core.database.models import MediaBuy as MediaBuyModel
    from tests.factories import MediaBuyFactory
    from tests.harness._base import IntegrationEnv

    with IntegrationEnv(tenant_id="t_1556_done", principal_id="p_1556_done") as env:
        tenant, principal = env.setup_default_data()
        buy = MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id="mb_1556_ready_completed",
            status="ready",
            start_date=(datetime.now(UTC) - timedelta(days=14)).date(),
            end_date=(datetime.now(UTC) - timedelta(hours=1)).date(),
            start_time=datetime.now(UTC) - timedelta(days=14),
            end_time=datetime.now(UTC) - timedelta(hours=1),
        )

        await MediaBuyStatusScheduler()._update_statuses()

        env.get_session().expire_all()
        row = env.get_one(MediaBuyModel, media_buy_id=buy.media_buy_id)
        assert row.status == "completed"


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_one_unprocessable_buy_does_not_strand_the_rest(integration_db):
    """A row that fails to compute must not block every other buy's transition.

    The sweep now covers the legacy serving aliases, so it reaches older rows whose
    flight fields were written under earlier conventions. Without a per-row guard a
    single bad row aborts the loop before ``session.commit()``, so every other buy
    silently stays in its transitional state — the failure is invisible because the
    outer handler logs and returns normally.
    """
    from unittest.mock import patch

    from src.core.database.models import MediaBuy as MediaBuyModel
    from tests.factories import MediaBuyFactory
    from tests.harness._base import IntegrationEnv

    with IntegrationEnv(tenant_id="t_strand", principal_id="p_strand") as env:
        tenant, principal = env.setup_default_data()
        common = {
            "tenant": tenant,
            "principal": principal,
            "status": "ready",
            "start_date": (datetime.now(UTC) - timedelta(hours=1)).date(),
            "end_date": (datetime.now(UTC) + timedelta(days=7)).date(),
            "start_time": datetime.now(UTC) - timedelta(hours=1),
            "end_time": datetime.now(UTC) + timedelta(days=7),
        }
        bad = MediaBuyFactory(media_buy_id="mb_strand_bad", **common)
        good = MediaBuyFactory(media_buy_id="mb_strand_good", **common)

        real = MediaBuyStatusScheduler._compute_new_status

        def raise_for_bad(self, media_buy, now, session):
            if media_buy.media_buy_id == bad.media_buy_id:
                raise ValueError("unprocessable legacy flight fields")
            # Delegate to the REAL computation so the good row is graded by
            # production logic, not by a stub that re-states the expectation.
            return real(self, media_buy, now, session)

        with patch.object(MediaBuyStatusScheduler, "_compute_new_status", raise_for_bad):
            await MediaBuyStatusScheduler()._update_statuses()

        env.get_session().expire_all()
        assert env.get_one(MediaBuyModel, media_buy_id=good.media_buy_id).status == "active", (
            "a sibling row failing to compute stranded this buy in 'ready' — the whole sweep aborted before committing"
        )
        assert env.get_one(MediaBuyModel, media_buy_id=bad.media_buy_id).status == "ready", (
            "the unprocessable row must be skipped, not transitioned on a guess"
        )


@pytest.mark.requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected_calls", "why"),
    [
        (ValueError("bad row data"), 3, "a row-level fault skips that row and keeps sweeping"),
        (
            SQLAlchemyError("connection lost"),
            1,
            "a session-level fault aborts the sweep: _compute_new_status queries creative "
            "approval, so a failed statement leaves the transaction aborted and continuing "
            "would only manufacture failures on rows that were never bad",
        ),
    ],
    ids=["row-level-continues", "session-level-aborts"],
)
async def test_row_faults_continue_but_session_faults_abort(integration_db, raised, expected_calls, why):
    """Grades the abort-vs-continue split directly, independent of row ordering."""
    from unittest.mock import patch

    from tests.factories import MediaBuyFactory
    from tests.harness._base import IntegrationEnv

    with IntegrationEnv(tenant_id="t_abort", principal_id="p_abort") as env:
        tenant, principal = env.setup_default_data()
        for i in range(3):
            MediaBuyFactory(
                tenant=tenant,
                principal=principal,
                media_buy_id=f"mb_abort_{i}",
                status="ready",
                start_date=(datetime.now(UTC) - timedelta(hours=1)).date(),
                end_date=(datetime.now(UTC) + timedelta(days=7)).date(),
                start_time=datetime.now(UTC) - timedelta(hours=1),
                end_time=datetime.now(UTC) + timedelta(days=7),
            )

        calls = []

        def always_raise(self, media_buy, now, session):
            calls.append(media_buy.media_buy_id)
            raise raised

        with patch.object(MediaBuyStatusScheduler, "_compute_new_status", always_raise):
            await MediaBuyStatusScheduler()._update_statuses()

        assert len(calls) == expected_calls, f"{why} (saw {len(calls)} calls: {calls})"


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_status_sweep_summary_accounts_for_every_selected_buy(integration_db):
    """The sweep summary's buckets partition its selection.

    The sibling invariant to ``TestBatchSummaryAccountsForEverySelectedBuy`` in
    test_delivery_poll_behavioral.py. The summary is what distinguishes "every row
    was skipped" from "there was nothing to do", and a path that leaves the loop
    without counting breaks that quietly — the remaining numbers still look
    plausible. Before this, only transitions were counted and the log line fired
    only when ``updated > 0``, so a sweep that selected rows and changed none
    emitted nothing at all.

    Asserts the identity (buckets sum to selection) rather than each bucket's
    value, so it keeps holding as outcomes are added.

    ``no_flight_window`` is NOT seeded here, and cannot be: MediaBuy.start_date and
    end_date are both NOT NULL, so ``_compute_new_status`` always resolves a window
    and that bucket is unreachable against the current schema (see
    ``_NoFlightWindow``). Seeding it fails at INSERT, which is the honest reason
    this asserts over the reachable buckets only.
    """
    from tests.factories import MediaBuyFactory
    from tests.harness._base import IntegrationEnv

    with IntegrationEnv(tenant_id="t_sweep_sum", principal_id="p_sweep_sum") as env:
        tenant, principal = env.setup_default_data()
        common = {"tenant": tenant, "principal": principal}
        mid_flight = {
            "start_date": (datetime.now(UTC) - timedelta(hours=1)).date(),
            "end_date": (datetime.now(UTC) + timedelta(days=7)).date(),
            "start_time": datetime.now(UTC) - timedelta(hours=1),
            "end_time": datetime.now(UTC) + timedelta(days=7),
        }

        # updated: mid-flight legacy alias -> active
        MediaBuyFactory(media_buy_id="mb_sweep_update", status="ready", **mid_flight, **common)
        # unchanged: already active mid-flight
        MediaBuyFactory(media_buy_id="mb_sweep_unchanged", status="active", **mid_flight, **common)

        summary = await MediaBuyStatusScheduler()._update_statuses()

        assert summary.selected >= 2, (
            f"expected both seeded buys to be selected, got {summary.selected} — "
            "the seeding no longer exercises the sweep"
        )
        assert summary.updated >= 1 and summary.unchanged >= 1, (
            f"expected the sweep to hit both a transition and a no-op "
            f"(updated={summary.updated} unchanged={summary.unchanged}) — "
            "a partition assertion over one bucket proves nothing"
        )
        assert summary.accounted_for == summary.selected, (
            f"buckets do not partition the selection: selected={summary.selected} "
            f"accounted_for={summary.accounted_for} (updated={summary.updated} "
            f"unchanged={summary.unchanged} no_flight_window={summary.no_flight_window} "
            f"errors={summary.errors}) — some path left the loop without counting"
        )


@pytest.mark.requires_db
@pytest.mark.asyncio
async def test_sweep_summary_is_logged_even_when_nothing_changed(integration_db):
    """A sweep that transitioned nothing must still say what it looked at.

    The twin of test_batch_summary_distinguishes_suppressed_from_idle on the
    delivery scheduler. The summary line used to be wrapped in
    ``if updated_count > 0``, so a sweep over any number of rows that needed no
    transition emitted NOTHING — indistinguishable in the log from a sweep that
    selected nothing at all. An operator cannot tell a healthy idle scheduler from
    one whose whole population is stuck.

    The counters were already graded by the partition test; this grades the LINE,
    which is the part an operator actually reads. Without it, re-wrapping the log
    in its old condition leaves the suite green — the code was swept and the test
    was not, which is the shape this whole round is about.

    Spies the module logger rather than using caplog, for the reason the sibling
    records: caplog depends on global logging state other tests mutate, so a
    caplog version passes alone and captures nothing in the full suite.
    """
    from unittest.mock import patch

    from tests.factories import MediaBuyFactory
    from tests.harness._base import IntegrationEnv

    with IntegrationEnv(tenant_id="t_sweep_log", principal_id="p_sweep_log") as env:
        tenant, principal = env.setup_default_data()
        # Already active mid-flight: selected, needs no transition.
        MediaBuyFactory(
            tenant=tenant,
            principal=principal,
            media_buy_id="mb_sweep_noop",
            status="active",
            start_date=(datetime.now(UTC) - timedelta(hours=1)).date(),
            end_date=(datetime.now(UTC) + timedelta(days=7)).date(),
            start_time=datetime.now(UTC) - timedelta(hours=1),
            end_time=datetime.now(UTC) + timedelta(days=7),
        )

        lines: list[str] = []

        def capture(msg, *args, **kwargs):
            lines.append(msg % args if args else msg)

        with patch("src.services.media_buy_status_scheduler.logger.info", side_effect=capture):
            summary = await MediaBuyStatusScheduler()._update_statuses()

        assert summary.updated == 0, f"seeding no longer produces a no-op sweep (updated={summary.updated})"
        sweep = [line for line in lines if "Status sweep:" in line]
        assert sweep, (
            f"a sweep that changed nothing emitted no summary line (saw {lines!r}) — "
            "silence here is indistinguishable from having selected nothing"
        )
        assert "1 unchanged" in sweep[-1], f"the selected-but-unchanged buy is invisible in the summary: {sweep[-1]!r}"
        assert "of 1 selected" in sweep[-1], (
            f"the summary must report against the selection, not just the buckets: {sweep[-1]!r}"
        )
