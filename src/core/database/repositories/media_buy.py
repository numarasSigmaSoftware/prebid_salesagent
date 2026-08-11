"""MediaBuy repository — tenant-scoped data access for media buys and packages.

Core invariant: every query includes tenant_id in the WHERE clause. The tenant_id
is set at construction time and injected into all queries automatically.

Cross-tenant queries (for schedulers) use class methods that explicitly accept a
session and do not enforce tenant isolation — these are system-level operations.

beads: salesagent-t735 (foundation), salesagent-2lp8 (epic), salesagent-to9i (admin/scheduler migration),
       salesagent-dyb6 (write methods)
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, joinedload

from src.core.database.models import MediaBuy, MediaPackage, WebhookDeliveryLog
from src.core.database.repositories.delivery import successful_final_log_clauses

if TYPE_CHECKING:
    from adcp.types import ContextObject, ReportingWebhook


class MediaBuyRepository:
    """Tenant-scoped data access for MediaBuy and MediaPackage.

    Instance queries filter by tenant_id automatically — callers cannot bypass
    tenant isolation through them. The ``@staticmethod`` scheduler queries
    (``get_all_by_statuses``, ``get_reportable_for_delivery``) are explicitly
    system-level and cross-tenant by design.

    Write methods add objects to the session but never commit — the Unit of Work
    (MediaBuyUoW) handles commit/rollback at the boundary.

    Args:
        session: SQLAlchemy session (caller manages lifecycle).
        tenant_id: Tenant scope for all queries.
    """

    _MEDIA_BUY_IMMUTABLE_FIELDS: frozenset[str] = frozenset({"tenant_id", "media_buy_id", "created_at"})
    _PACKAGE_IMMUTABLE_FIELDS: frozenset[str] = frozenset({"media_buy_id", "package_id"})

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @staticmethod
    def _claim_is_available(column, stale_before: datetime.datetime):
        """A claim column is takeable when unset, or held past its lease.

        One expression, used for BOTH claim columns by BOTH claim methods, so
        "is this claim free?" cannot come to mean two different things depending
        on which method you read.
        """
        return or_(column.is_(None), column < stale_before)

    def try_claim_final_webhook(
        self,
        media_buy_id: str,
        *,
        now: datetime.datetime,
        stale_before: datetime.datetime,
        periodic_stale_before: datetime.datetime,
    ) -> bool:
        """Atomically claim this buy's FINAL delivery webhook. True if THIS caller won.

        Best-effort concurrency guard: a single conditional UPDATE that sets
        ``final_webhook_claimed_at = now`` only when it is unset OR older than
        ``stale_before`` (so a crashed worker's claim self-heals once stale rather
        than stranding the final forever). Two concurrent workers race on the same
        row; exactly one UPDATE matches and RETURNs the id — the loser matches 0 rows
        and skips the send. The caller MUST commit for the claim to be visible to
        other transactions. This does NOT close the crash-after-POST duplicate window
        (the POST precedes the success-log write); a durable exactly-once final
        (outbox) is tracked in #1606.

        The claim is MUTUALLY EXCLUSIVE with the periodic claim: it also requires
        ``last_periodic_webhook_claimed_at`` to be free or past
        ``periodic_stale_before``. Two columns that each serialize their own type
        serialize nothing against each other, and the two types are not decided
        from a shared snapshot -- each worker computes ``is_final`` from the
        MediaBuy row and the current UTC date as IT read them, so a status flip or
        a midnight rollover between two workers' reads yields one final and one
        periodic worker for the same buy. Both would have won their own column,
        both would have POSTed, each with a different idempotency_key (the key is
        derived from notification_type), and both carrying the SAME
        sequence_number -- the buyer-visible duplicate this claim exists to
        prevent, arriving through the one door it did not cover.

        Blocking is bounded: both leases are far shorter than the 24h periodic
        dedup, so a periodic legitimately following a successful final is already
        suppressed by that dedup, and a final that loses to an in-flight periodic
        is re-selected by the next batch (the anti-join keeps returning the buy
        until a successful final row exists).
        """
        claimed_id = self._session.execute(
            update(MediaBuy)
            .where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaBuy.media_buy_id == media_buy_id,
                self._claim_is_available(MediaBuy.final_webhook_claimed_at, stale_before),
                self._claim_is_available(MediaBuy.last_periodic_webhook_claimed_at, periodic_stale_before),
            )
            .values(final_webhook_claimed_at=now)
            .returning(MediaBuy.media_buy_id)
        ).scalar_one_or_none()
        return claimed_id is not None

    def release_final_webhook_claim(self, media_buy_id: str, *, claimed_at: datetime.datetime) -> bool:
        """Release THIS worker's final-webhook claim so a definitive failure/no-send
        doesn't block an immediate retry for the whole lease. True if the claim was cleared.

        Token-guarded: clears ``final_webhook_claimed_at`` only when it still
        equals ``claimed_at`` — the exact timestamp this worker wrote in
        ``try_claim_final_webhook``. If the lease already expired and another worker
        re-claimed with a newer timestamp, the ``== claimed_at`` predicate matches 0
        rows, so this never clears a newer owner's claim. Lease recovery still covers
        an actual crash (where no release runs). The caller MUST commit.
        """
        released_id = self._session.execute(
            update(MediaBuy)
            .where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaBuy.media_buy_id == media_buy_id,
                MediaBuy.final_webhook_claimed_at == claimed_at,
            )
            .values(final_webhook_claimed_at=None)
            .returning(MediaBuy.media_buy_id)
        ).scalar_one_or_none()
        return released_id is not None

    def try_claim_periodic_webhook(
        self,
        media_buy_id: str,
        *,
        now: datetime.datetime,
        stale_before: datetime.datetime,
        final_stale_before: datetime.datetime,
    ) -> bool:
        """Atomically claim this buy's PERIODIC delivery webhook. True if THIS caller won.

        Same pattern as ``try_claim_final_webhook``, for the non-final (periodic)
        send path: a single conditional UPDATE that sets
        ``last_periodic_webhook_claimed_at = now`` only when it is unset OR older
        than ``stale_before``, so a crashed worker's claim self-heals rather than
        stranding the buy's next periodic report. Two concurrent workers (e.g. two
        replicas under horizontal scaling) race on the same row; exactly one
        UPDATE matches and RETURNs the id — the loser matches 0 rows and skips the
        send. The 24h dedup in ``_should_skip_send`` is a separate, READ-ONLY
        check against successful send history; this claim closes the race window
        between that check and the POST, it does not replace the dedup itself.
        The caller MUST commit for the claim to be visible to other transactions.

        Mutually exclusive with the final claim (it also requires
        ``final_webhook_claimed_at`` to be free or past ``final_stale_before``) —
        see ``try_claim_final_webhook`` for why per-type columns alone let a
        final and a periodic worker both POST with the same sequence_number.
        """
        claimed_id = self._session.execute(
            update(MediaBuy)
            .where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaBuy.media_buy_id == media_buy_id,
                self._claim_is_available(MediaBuy.last_periodic_webhook_claimed_at, stale_before),
                self._claim_is_available(MediaBuy.final_webhook_claimed_at, final_stale_before),
            )
            .values(last_periodic_webhook_claimed_at=now)
            .returning(MediaBuy.media_buy_id)
        ).scalar_one_or_none()
        return claimed_id is not None

    def release_periodic_webhook_claim(self, media_buy_id: str, *, claimed_at: datetime.datetime) -> bool:
        """Release THIS worker's periodic-webhook claim so a definitive failure/no-send
        doesn't block an immediate retry for the whole lease. True if the claim was cleared.

        Token-guarded: clears ``last_periodic_webhook_claimed_at`` only when it
        still equals ``claimed_at`` — the exact timestamp this worker wrote in
        ``try_claim_periodic_webhook``. If the lease already expired and another
        worker re-claimed with a newer timestamp, the ``== claimed_at`` predicate
        matches 0 rows, so this never clears a newer owner's claim. Lease recovery
        still covers an actual crash (where no release runs). The caller MUST commit.
        """
        released_id = self._session.execute(
            update(MediaBuy)
            .where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaBuy.media_buy_id == media_buy_id,
                MediaBuy.last_periodic_webhook_claimed_at == claimed_at,
            )
            .values(last_periodic_webhook_claimed_at=None)
            .returning(MediaBuy.media_buy_id)
        ).scalar_one_or_none()
        return released_id is not None

    # ------------------------------------------------------------------
    # Single MediaBuy lookups
    # ------------------------------------------------------------------

    def get_by_id(self, media_buy_id: str) -> MediaBuy | None:
        """Get a media buy by its ID within the tenant."""
        return self._session.scalars(
            select(MediaBuy).where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaBuy.media_buy_id == media_buy_id,
            )
        ).first()

    def get_by_id_or_raise(
        self, media_buy_id: str, *, context: ContextObject | dict[str, Any] | None = None
    ) -> MediaBuy:
        """Get a media buy by ID or raise ``AdCPMediaBuyNotFoundError``.

        Collapses the "look up the media buy, raise the typed not-found if it
        does not exist" guard duplicated across the update tool into one place.
        ``context`` is echoed into the error envelope so buyer agents can
        correlate the failure. Coexists with ``get_by_id`` — callers that
        deliberately tolerate ``None`` keep using that.
        """
        media_buy = self.get_by_id(media_buy_id)
        if media_buy is None:
            from src.core.exceptions import AdCPMediaBuyNotFoundError

            raise AdCPMediaBuyNotFoundError(
                f"Media buy '{media_buy_id}' not found",
                suggestion="Verify the media_buy_id is correct and belongs to your account.",
                context=context,
            )
        return media_buy

    def find_by_idempotency_key(
        self, idempotency_key: str, principal_id: str, account_id: str | None = None
    ) -> MediaBuy | None:
        """Find an existing media buy by idempotency_key within (tenant, principal, account).

        The AdCP idempotency scope is (agent, account, key): the same key under a
        different account is an independent request, never a hit. ``account_id is
        None`` matches rows stored with no account (``IS NULL``), mirroring the
        NULLS NOT DISTINCT unique backstop index.
        """
        return self._session.scalars(
            select(MediaBuy).where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaBuy.principal_id == principal_id,
                # SQLAlchemy renders ``== None`` as ``IS NULL`` — matches no-account rows.
                MediaBuy.account_id == account_id,
                MediaBuy.idempotency_key == idempotency_key,
            )
        ).first()

    def get_by_id_or_idempotency_key(
        self, identifier: str, principal_id: str, account_id: str | None = None
    ) -> MediaBuy | None:
        """Get a media buy by ID first, then fall back to idempotency_key.

        ``account_id`` scopes the idempotency-key fallback to the spec's
        (agent, account, key) tuple. It is threaded through to
        ``find_by_idempotency_key`` rather than dropped — otherwise the
        fallback would silently match only no-account (``IS NULL``) rows.
        """
        result = self.get_by_id(identifier)
        if result is None:
            result = self.find_by_idempotency_key(identifier, principal_id, account_id=account_id)
        return result

    # ------------------------------------------------------------------
    # List queries
    # ------------------------------------------------------------------

    def get_by_principal(
        self,
        principal_id: str,
        *,
        media_buy_ids: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[MediaBuy]:
        """Get media buys for a principal within the tenant.

        Filters are combined with AND. Pass None to skip a filter.
        """
        stmt = select(MediaBuy).where(
            MediaBuy.tenant_id == self._tenant_id,
            MediaBuy.principal_id == principal_id,
        )
        if media_buy_ids is not None:
            stmt = stmt.where(MediaBuy.media_buy_id.in_(media_buy_ids))
        if statuses is not None:
            stmt = stmt.where(MediaBuy.status.in_(statuses))
        return list(self._session.scalars(stmt).all())

    # ------------------------------------------------------------------
    # Package queries — tenant isolation through MediaBuy FK join
    # ------------------------------------------------------------------

    def get_packages(self, media_buy_id: str) -> list[MediaPackage]:
        """Get all packages for a media buy, verified to belong to this tenant.

        Joins through MediaBuy to enforce tenant isolation — MediaPackage has
        no tenant_id column, so we verify via the parent MediaBuy.
        """
        return list(
            self._session.scalars(
                select(MediaPackage)
                .join(MediaBuy, MediaPackage.media_buy_id == MediaBuy.media_buy_id)
                .where(
                    MediaBuy.tenant_id == self._tenant_id,
                    MediaPackage.media_buy_id == media_buy_id,
                )
            ).all()
        )

    def get_package(self, media_buy_id: str, package_id: str) -> MediaPackage | None:
        """Get a specific package, verified to belong to this tenant."""
        return self._session.scalars(
            select(MediaPackage)
            .join(MediaBuy, MediaPackage.media_buy_id == MediaBuy.media_buy_id)
            .where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaPackage.media_buy_id == media_buy_id,
                MediaPackage.package_id == package_id,
            )
        ).first()

    def get_package_or_raise(
        self, media_buy_id: str, package_id: str, *, context: ContextObject | dict[str, Any] | None = None
    ) -> MediaPackage:
        """Get a package or raise ``AdCPPackageNotFoundError``.

        Collapses the package fetch-and-raise guard duplicated across the update
        tool. ``context`` is echoed into the error envelope. Coexists with
        ``get_package`` for callers that tolerate ``None``.
        """
        package = self.get_package(media_buy_id, package_id)
        if package is None:
            from src.core.exceptions import AdCPPackageNotFoundError

            raise AdCPPackageNotFoundError(
                f"Package '{package_id}' not found for media buy '{media_buy_id}'",
                suggestion="Verify the package_id exists in this media buy; list the media buy's packages to find valid ids.",
                context=context,
            )
        return package

    def get_packages_for_ids(self, media_buy_ids: list[str]) -> dict[str, list[MediaPackage]]:
        """Get packages for multiple media buys, grouped by media_buy_id.

        Only returns packages for media buys belonging to this tenant.
        Media buy IDs not belonging to this tenant are silently excluded.
        """
        if not media_buy_ids:
            return {}

        packages = self._session.scalars(
            select(MediaPackage)
            .join(MediaBuy, MediaPackage.media_buy_id == MediaBuy.media_buy_id)
            .where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaPackage.media_buy_id.in_(media_buy_ids),
            )
        ).all()

        result: dict[str, list[MediaPackage]] = {}
        for pkg in packages:
            result.setdefault(pkg.media_buy_id, []).append(pkg)
        return result

    def find_package_with_media_buy(self, package_id: str) -> tuple[MediaPackage, MediaBuy] | None:
        """Find a package and its parent media buy by package_id within the tenant.

        Useful when you only have a package_id and need to resolve the parent
        media buy (e.g. during creative-to-package assignment).

        Returns (MediaPackage, MediaBuy) tuple or None if not found.
        """
        result = self._session.execute(
            select(MediaPackage, MediaBuy)
            .join(MediaBuy, MediaPackage.media_buy_id == MediaBuy.media_buy_id)
            .where(
                MediaPackage.package_id == package_id,
                MediaBuy.tenant_id == self._tenant_id,
            )
        ).first()
        if result is None:
            return None
        return result[0], result[1]

    # ------------------------------------------------------------------
    # Tenant-wide list queries (for admin/dashboard)
    # ------------------------------------------------------------------

    def list_all(self) -> list[MediaBuy]:
        """Get all media buys for the tenant."""
        return list(self._session.scalars(select(MediaBuy).where(MediaBuy.tenant_id == self._tenant_id)).all())

    def list_by_statuses(self, statuses: list[str]) -> list[MediaBuy]:
        """Get media buys for the tenant filtered by status list."""
        return list(
            self._session.scalars(
                select(MediaBuy).where(
                    MediaBuy.tenant_id == self._tenant_id,
                    MediaBuy.status.in_(statuses),
                )
            ).all()
        )

    def list_recent(
        self,
        limit: int = 10,
        *,
        eager_load_principal: bool = False,
    ) -> list[MediaBuy]:
        """Get the most recent media buys for the tenant, ordered by created_at desc."""
        stmt = (
            select(MediaBuy)
            .where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaBuy.media_buy_id.isnot(None),
            )
            .order_by(MediaBuy.created_at.desc())
            .limit(limit)
        )
        if eager_load_principal:
            stmt = stmt.options(joinedload(MediaBuy.principal))
        return list(self._session.scalars(stmt).all())

    def list_in_flight_on_date(
        self,
        target_date: datetime.date,
        statuses: list[str] | None = None,
    ) -> list[MediaBuy]:
        """Get media buys whose flight period covers target_date.

        Useful for revenue trend calculations.
        """
        stmt = select(MediaBuy).where(
            MediaBuy.tenant_id == self._tenant_id,
            MediaBuy.start_date <= target_date,
            MediaBuy.end_date >= target_date,
        )
        if statuses:
            stmt = stmt.where(MediaBuy.status.in_(statuses))
        return list(self._session.scalars(stmt).all())

    def list_all_ordered_by_created(self) -> list[MediaBuy]:
        """Get all media buys for the tenant, ordered by created_at desc."""
        return list(
            self._session.scalars(
                select(MediaBuy).where(MediaBuy.tenant_id == self._tenant_id).order_by(MediaBuy.created_at.desc())
            ).all()
        )

    # ------------------------------------------------------------------
    # MediaBuy writes
    # ------------------------------------------------------------------

    def create_from_request(
        self,
        *,
        media_buy_id: str,
        req: Any,
        principal_id: str,
        advertiser_name: str,
        budget: Decimal | float,
        currency: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        status: str = "draft",
        order_name: str | None = None,
        campaign_objective: str | None = None,
        kpi_goal: str | None = None,
        package_id_map: dict[int, str] | None = None,
        by_alias: bool = False,
        created_at: datetime.datetime | None = None,
        account_id: str | None = None,
        payload_hash: str | None = None,
    ) -> MediaBuy:
        """Create a MediaBuy from a request model, serializing raw_request at the DB boundary.

        This is the preferred method for creating media buys from _impl functions.
        The request model is serialized here (not in business logic) per the
        no-model-dump-in-impl architectural principle.

        Args:
            media_buy_id: Unique media buy identifier.
            req: CreateMediaBuyRequest Pydantic model (serialized here, not by caller).
            principal_id: Principal ID for ownership.
            advertiser_name: Display name of the advertiser.
            budget: Total budget for the media buy.
            currency: Currency code (e.g., "USD").
            start_time: Campaign start time.
            end_time: Campaign end time.
            status: Initial status (default: "draft").
            order_name: Order name (defaults to req.po_number or "Order-{id}").
            campaign_objective: Optional campaign objective.
            kpi_goal: Optional KPI goal.
            package_id_map: Map of package index → package_id to inject into serialized packages.
            by_alias: Whether to serialize with field aliases (e.g., content_uri).
            created_at: Optional explicit created_at timestamp.
            account_id: Resolved account scope (AdCP idempotency scope is agent+account+key).
            payload_hash: Canonical request hash from the idempotency probe; the
                degraded fallback's IDEMPOTENCY_CONFLICT signal.

        Returns:
            The created MediaBuy ORM object (added to session, not committed).
        """
        raw = req.model_dump(mode="json", by_alias=by_alias)
        if package_id_map:
            packages = raw.get("packages", [])
            for idx, pkg_id in package_id_map.items():
                if idx < len(packages):
                    packages[idx]["package_id"] = pkg_id

        kwargs: dict[str, Any] = {
            "media_buy_id": media_buy_id,
            "tenant_id": self._tenant_id,
            "principal_id": principal_id,
            "idempotency_key": getattr(req, "idempotency_key", None),
            "order_name": order_name or getattr(req, "po_number", None) or f"Order-{media_buy_id}",
            "advertiser_name": advertiser_name,
            "budget": budget,
            "currency": currency,
            "start_date": start_time.date(),
            "end_date": end_time.date(),
            "start_time": start_time,
            "end_time": end_time,
            "status": status,
            "raw_request": raw,
            # Canonical request hash as computed by the idempotency probe —
            # raw_request is not canonicalizable (injected package_ids,
            # alias-dependent names), so the degraded idempotency fallback
            # conflict-checks against this stored hash.
            "payload_hash": payload_hash,
        }
        if campaign_objective is not None:
            kwargs["campaign_objective"] = campaign_objective
        if kpi_goal is not None:
            kwargs["kpi_goal"] = kpi_goal
        if created_at is not None:
            kwargs["created_at"] = created_at
        if account_id is not None:
            kwargs["account_id"] = account_id

        media_buy = MediaBuy(**kwargs)
        self._session.add(media_buy)
        self._session.flush()
        return media_buy

    def create(self, media_buy: MediaBuy) -> MediaBuy:
        """Persist a new media buy within this tenant.

        The media_buy.tenant_id must match the repository's tenant_id.
        Raises ValueError if there is a tenant mismatch.

        Does NOT commit — the UoW handles that.
        """
        if media_buy.tenant_id != self._tenant_id:
            raise ValueError(
                f"Tenant mismatch: media_buy.tenant_id={media_buy.tenant_id!r} "
                f"!= repository tenant_id={self._tenant_id!r}"
            )
        self._session.add(media_buy)
        self._session.flush()
        return media_buy

    def update_status(
        self,
        media_buy_id: str,
        status: str,
        *,
        approved_at: datetime.datetime | None = None,
        approved_by: str | None = None,
    ) -> MediaBuy | None:
        """Update the status of a media buy within this tenant.

        Returns the updated MediaBuy, or None if not found in this tenant.
        """
        media_buy = self.get_by_id(media_buy_id)
        if media_buy is None:
            return None
        media_buy.status = status
        if approved_at is not None:
            media_buy.approved_at = approved_at
        if approved_by is not None:
            media_buy.approved_by = approved_by
        self._session.flush()
        return media_buy

    def update_fields(self, media_buy_id: str, **kwargs: Any) -> MediaBuy | None:
        """Update arbitrary fields on a media buy within this tenant.

        Only updates fields that are valid MediaBuy column attributes.
        Returns the updated MediaBuy, or None if not found in this tenant.
        Raises ValueError if any kwarg is not a valid MediaBuy attribute or
        if the caller attempts to update an immutable field (tenant_id,
        media_buy_id, created_at).
        """
        blocked = self._MEDIA_BUY_IMMUTABLE_FIELDS & kwargs.keys()
        if blocked:
            raise ValueError(f"Cannot update immutable field(s): {', '.join(sorted(blocked))}")
        media_buy = self.get_by_id(media_buy_id)
        if media_buy is None:
            return None
        for key, value in kwargs.items():
            if not hasattr(media_buy, key):
                raise ValueError(f"MediaBuy has no attribute {key!r}")
            setattr(media_buy, key, value)
        self._session.flush()
        return media_buy

    def update_reporting_webhook(
        self,
        media_buy_id: str,
        reporting_webhook: ReportingWebhook,
    ) -> MediaBuy | None:
        """Persist a replacement reporting webhook in the media buy request snapshot.

        ``raw_request`` is a JSON column, so assign a copied mapping rather than
        mutating the existing dict in place. This makes SQLAlchemy's change
        tracking deterministic and preserves every unrelated request field.
        """
        media_buy = self.get_by_id(media_buy_id)
        if media_buy is None:
            return None

        raw_request = dict(media_buy.raw_request or {})
        raw_request["reporting_webhook"] = reporting_webhook.model_dump(mode="json", exclude_none=True)
        media_buy.raw_request = raw_request
        self._session.flush()
        return media_buy

    # ------------------------------------------------------------------
    # MediaPackage writes
    # ------------------------------------------------------------------

    def create_package(
        self,
        media_buy_id: str,
        package_id: str,
        package_config: dict,
        *,
        budget: Decimal | None = None,
        bid_price: Decimal | None = None,
        pacing: str | None = None,
    ) -> MediaPackage:
        """Create a new package for a media buy within this tenant.

        Verifies the parent media buy belongs to this tenant before creating.
        Raises ValueError if the media buy is not found in this tenant.
        """
        media_buy = self.get_by_id(media_buy_id)
        if media_buy is None:
            raise ValueError(f"MediaBuy {media_buy_id!r} not found in tenant {self._tenant_id!r}")
        package = MediaPackage(
            media_buy_id=media_buy_id,
            package_id=package_id,
            package_config=package_config,
            budget=budget,
            bid_price=bid_price,
            pacing=pacing,
        )
        self._session.add(package)
        self._session.flush()
        return package

    def update_package_config(
        self,
        media_buy_id: str,
        package_id: str,
        package_config: dict,
    ) -> MediaPackage | None:
        """Update the package_config of a package within this tenant.

        Returns the updated MediaPackage, or None if not found.
        """
        package = self.get_package(media_buy_id, package_id)
        if package is None:
            return None
        package.package_config = package_config
        self._session.flush()
        return package

    def update_package_fields(
        self,
        media_buy_id: str,
        package_id: str,
        **kwargs: Any,
    ) -> MediaPackage | None:
        """Update arbitrary fields on a package within this tenant.

        Only updates fields that are valid MediaPackage column attributes.
        Returns the updated MediaPackage, or None if not found.
        Raises ValueError if any kwarg is not a valid MediaPackage attribute or
        if the caller attempts to update an immutable field (media_buy_id,
        package_id).
        """
        blocked = self._PACKAGE_IMMUTABLE_FIELDS & kwargs.keys()
        if blocked:
            raise ValueError(f"Cannot update immutable field(s): {', '.join(sorted(blocked))}")
        package = self.get_package(media_buy_id, package_id)
        if package is None:
            return None
        for key, value in kwargs.items():
            if not hasattr(package, key):
                raise ValueError(f"MediaPackage has no attribute {key!r}")
            setattr(package, key, value)
        self._session.flush()
        return package

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def create_packages_bulk(
        self,
        media_buy_id: str,
        packages: list[MediaPackage],
    ) -> list[MediaPackage]:
        """Create multiple packages for a media buy within this tenant.

        Verifies the parent media buy belongs to this tenant before creating.
        Each package's media_buy_id must match the provided media_buy_id.
        Raises ValueError if the media buy is not found or if any package
        has a mismatched media_buy_id.
        """
        media_buy = self.get_by_id(media_buy_id)
        if media_buy is None:
            raise ValueError(f"MediaBuy {media_buy_id!r} not found in tenant {self._tenant_id!r}")
        for pkg in packages:
            if pkg.media_buy_id != media_buy_id:
                raise ValueError(
                    f"Package {pkg.package_id!r} has media_buy_id={pkg.media_buy_id!r} but expected {media_buy_id!r}"
                )
            self._session.add(pkg)
        self._session.flush()
        return packages

    # ------------------------------------------------------------------
    # Cross-tenant queries (for system-level schedulers)
    # ------------------------------------------------------------------

    @staticmethod
    def get_all_by_statuses(session: Session, statuses: list[str]) -> list[MediaBuy]:
        """Get media buys across ALL tenants filtered by status.

        This is a system-level query for schedulers that need to process
        media buys regardless of tenant. Not tenant-scoped.
        """
        return list(session.scalars(select(MediaBuy).where(MediaBuy.status.in_(statuses))).all())

    @staticmethod
    def get_reportable_for_delivery(
        session: Session,
        *,
        serving_statuses: list[str],
        terminal_statuses: list[str],
    ) -> list[MediaBuy]:
        """Select webhook-configured serving buys and terminal buys whose final is still unsent.

        Args:
            serving_statuses: PERSISTED statuses (``PERSISTED_STATUS_TO_CANONICAL`` keys)
                that mean "still serving" — selected while a reporting_webhook is
                configured (not anti-joined: a serving buy has no "final" to have
                already sent, so there is nothing to correlate against).
            terminal_statuses: PERSISTED completed/canceled/rejected statuses,
                selected while a reporting_webhook is configured AND until a
                successful final delivery log exists. Both arms take the persisted
                vocabulary because ``MediaBuy.status`` stores a map key, never a
                canonical/wire value.

        System-level cross-tenant query for the delivery webhook scheduler ONLY (the
        status scheduler keeps the unbounded ``get_all_by_statuses`` — its selection
        is lifecycle-bounded by construction). The correlated success-log anti-join
        is the durable correctness boundary: scheduler downtime cannot age an
        unsent final out of selection, while a successful final permanently removes
        the buy from later batches. The existing media-buy log index supports the
        correlated lookup. ``final_webhook_claimed_at`` is deliberately not a
        selection predicate because stale-lease recovery requires claimed rows to
        remain selectable after a crashed send.

        The reporting_webhook-configured requirement is a single top-level predicate
        rather than duplicated into each arm of the OR below — the prior duplicated
        form independently applied it only to the terminal arm, silently letting the
        serving arm select and materialize every serving buy regardless of webhook
        configuration. A single shared predicate can't drift out of sync with a new
        arm the way a per-arm copy already did once. Callers still re-check
        ``reporting_webhook`` truthiness in Python (see delivery_webhook_scheduler.py)
        because this SQL predicate is JSON key-*presence*, not truthiness — a
        reporting_webhook value of JSON null/false/{} is non-NULL here but still
        falsy there.
        """
        # Same predicate DeliveryRepository.has_successful_final applies as a
        # pre-send skip — correlated here against the outer select(MediaBuy)
        # instead of a caller-known scalar id, so the two gates cannot diverge.
        successful_final_exists = (
            select(WebhookDeliveryLog.id)
            .where(
                *successful_final_log_clauses(
                    tenant_id=MediaBuy.tenant_id,
                    media_buy_id=MediaBuy.media_buy_id,
                )
            )
            .exists()
        )
        return list(
            session.scalars(
                select(MediaBuy).where(
                    MediaBuy.raw_request["reporting_webhook"].is_not(None),
                    or_(
                        MediaBuy.status.in_(serving_statuses),
                        MediaBuy.status.in_(terminal_statuses) & ~successful_final_exists,
                    ),
                )
            ).all()
        )
