"""MediaBuy repository — tenant-scoped data access for media buys and packages.

Core invariant: every query includes tenant_id in the WHERE clause. The tenant_id
is set at construction time and injected into all queries automatically.

Cross-tenant queries (for schedulers) use class methods that explicitly accept a
session and do not enforce tenant isolation — these are system-level operations.

        (write methods)
"""

from __future__ import annotations

import datetime
from collections.abc import Collection
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, joinedload

from src.core.database.models import (
    MediaBuy,
    MediaPackage,
    PersistedMediaBuyStatus,
)
from src.core.exceptions import AdCPInvariantViolationError, AdCPRevisionConflictError

if TYPE_CHECKING:
    from adcp.types import ContextObject

#: How long the revision re-read waits for the row lock before giving up. Long enough
#: that an ordinary in-flight update finishes first, short enough that a buyer gets an
#: answer instead of a hung connection.
_REVISION_LOCK_TIMEOUT_MS = 3000

#: PostgreSQL SQLSTATE for "could not obtain lock" -- what lock_timeout raises.
_LOCK_NOT_AVAILABLE = "55P03"


class MediaBuyRepository:
    """Tenant-scoped data access for MediaBuy and MediaPackage.

    All queries filter by tenant_id automatically. Callers cannot bypass
    tenant isolation — there is no way to query across tenants.

    Write methods add objects to the session but never commit — the Unit of Work
    (MediaBuyUoW) handles commit/rollback at the boundary.

    Args:
        session: SQLAlchemy session (caller manages lifecycle).
        tenant_id: Tenant scope for all queries.
    """

    # "revision" and "confirmed_at" are repository-managed — "revision" advances once
    # per successful mutation (see _bump_revision) and "confirmed_at" is stamped
    # write-once the first time the buy reaches a committed status (see
    # _stamp_confirmation_if_needed). Callers may never write either directly:
    # letting update_fields set confirmed_at would walk straight through the
    # write-once guard, because the stamp helper no-ops on an already-set value.
    _MEDIA_BUY_IMMUTABLE_FIELDS: frozenset[str] = frozenset(
        {"tenant_id", "media_buy_id", "created_at", "revision", "confirmed_at"}
    )
    _PACKAGE_IMMUTABLE_FIELDS: frozenset[str] = frozenset({"media_buy_id", "package_id"})

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id
        #: ``{media_buy_id: revision}`` — the value ``compare_and_set_revision`` advanced
        #: a buy TO in this unit of work. The next write to that buy spends the advance
        #: instead of taking its own, but only while the row still carries the recorded
        #: value — see ``_bump_revision``.
        self._prepaid_revision_advance: dict[str, int] = {}

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

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
        statuses: list[PersistedMediaBuyStatus] | None = None,
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

    def get_active(self) -> list[MediaBuy]:
        """Get all active media buys for the tenant."""
        return list(
            self._session.scalars(
                select(MediaBuy).where(
                    MediaBuy.tenant_id == self._tenant_id,
                    MediaBuy.status.in_(["active", "approved"]),
                )
            ).all()
        )

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

    def list_by_statuses(self, statuses: list[PersistedMediaBuyStatus]) -> list[MediaBuy]:
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
        statuses: list[PersistedMediaBuyStatus] | None = None,
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
        seller_committed: bool = False,
        media_buy_id: str,
        req: Any,
        principal_id: str,
        advertiser_name: str,
        budget: Decimal | float,
        currency: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        status: PersistedMediaBuyStatus = PersistedMediaBuyStatus.DRAFT,
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
            "status": PersistedMediaBuyStatus.parse(status, media_buy_id=media_buy_id),
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
        self._stamp_confirmation_if_needed(media_buy, seller_committed=seller_committed)
        self._session.add(media_buy)
        self._session.flush()
        return media_buy

    def create(self, media_buy: MediaBuy, *, seller_committed: bool = False) -> MediaBuy:
        """Persist a new media buy within this tenant.

        The media_buy.tenant_id must match the repository's tenant_id.
        Raises ValueError if there is a tenant mismatch.

        Does NOT commit — the UoW handles that.

        Stamps ``confirmed_at`` before the flush, so a caller-built row that is
        already in a committed status cannot be persisted with a NULL
        seller-commitment instant. This is a forward-lock rather than a fix for a
        live path: today every production create goes through
        ``create_from_request`` (this method has no production callers), but both
        entry points must hold the invariant or the next caller to pick this one
        reopens the hole.
        """
        if media_buy.tenant_id != self._tenant_id:
            raise ValueError(
                f"Tenant mismatch: media_buy.tenant_id={media_buy.tenant_id!r} "
                f"!= repository tenant_id={self._tenant_id!r}"
            )
        # The caller built this row itself, so its status column is still a raw
        # string; parse it at the door like any other untyped input.
        media_buy.status = PersistedMediaBuyStatus.parse(media_buy.status, media_buy_id=media_buy.media_buy_id)
        self._stamp_confirmation_if_needed(media_buy, seller_committed=seller_committed)
        self._session.add(media_buy)
        self._session.flush()
        return media_buy

    def _bump_revision(self, media_buy: MediaBuy) -> None:
        """Advance the persisted monotonic revision counter by one.

        Unless ``compare_and_set_revision`` already paid for this one. Honouring a
        buyer's optimistic-concurrency token must not cost the buyer an extra advance:
        the pinned update-media-buy-response.json calls the value it reports "Revision
        number after this update" — singular — and a buy at 7 updated with token 7 was
        answering 9, because the compare-and-set advanced it and then the field write
        advanced it again. The buyer's next token was two ahead of the single increment
        the response described.

        So the compare-and-set PREPAYS one advance (it must: it returns the new value,
        and its own caller may write nothing at all), and the next write to that buy in
        the same unit of work draws on the prepayment rather than taking a second
        advance. The resulting invariant is that supplying a token does not change how
        many times a request advances the revision — a request with N writes advances N
        times either way.

        The prepayment records the VALUE it advanced to, and is honoured only while the
        row still carries that value. A bare "already advanced" flag would be wrong,
        because the prepayment can be undone underneath us: the update flow's nested
        ``get_db_session()`` blocks share the scoped session and CLOSE it mid-request,
        which rolls the compare-and-set's UPDATE back unless one of them committed first.
        Measured on two consecutive updates in one process, that goes both ways. Against
        a flag, the second one declined to advance for a prepayment that no longer
        existed and the buyer's token stood still; checking the value means a lost
        prepayment is simply re-taken.

        The credit is scoped to THIS repository, which is the unit of work: ``MediaBuyUoW``
        builds a fresh repository on every ``__enter__``, and every other construction
        site builds one inside a single ``with get_db_session()`` block. It is consumed
        by the first write either way, so two sequential writes still advance twice.

        Assigning a SQL expression rather than doing a Python read-modify-write is
        deliberate: it emits ``UPDATE ... SET revision = revision + 1``, so the
        database serializes concurrent bumps. A read-modify-write would let
        two mutations that read the same value write the same value, and ``revision``
        is the buyer's optimistic-concurrency token — it MUST strictly increase on
        every successful mutation, including two landing in the same clock tick.

        The attribute holds a SQL expression until the next refresh, so a caller
        reading ``media_buy.revision`` between this call and the flush gets that
        expression object rather than an int.

        That does NOT announce itself. Measured across ten read shapes, only two raise
        — ``int(x)`` and ``bool(x)``. Arithmetic and comparison, the two an earlier
        version of this docstring named as the raising cases, silently build a further
        SQL expression: ``x + 1`` and ``x > 2`` both succeed and return an object, not a
        number.

        The ground that actually holds the invariant is the call sites, not the type.
        Four of the five direct callers flush on the very next line, and the fifth is
        ``_bump_parent_revision``, whose own two callers both flush on their next line.
        So no exercised path reads the attribute while it holds an expression. There is
        no guard here because the window is closed by construction rather than detected
        — but if a future caller stops flushing, nothing will raise.
        """
        # Popped whether or not it is honoured: a prepayment pays for one write, and one
        # that turns out to have been rolled back is spent rather than carried forward.
        # ``isinstance`` guards the case this method itself creates -- an unflushed bump
        # leaves ``revision`` holding a SQL expression, and comparing one to an int
        # builds a further expression instead of answering.
        prepaid = self._prepaid_revision_advance.pop(media_buy.media_buy_id, None)
        if prepaid is not None and isinstance(media_buy.revision, int) and media_buy.revision == prepaid:
            return
        media_buy.revision = MediaBuy.revision + 1

    @staticmethod
    def _stamp_confirmation_if_needed(media_buy: MediaBuy, *, seller_committed: bool) -> bool:
        """Write ``confirmed_at`` the first time the seller actually commits.

        Write-once by design: ``confirmed_at`` is the instant the seller committed to
        running the buy, so it must stay stable across every later transition rather
        than tracking the most recent one. Returns whether it stamped.

        ``seller_committed`` is passed by the caller and is NOT inferred from status.
        It used to be ``is_media_buy_seller_confirmed(media_buy.status)``, and that
        proxy is lossy at exactly one member: ``media_buy_create._compute_status``
        returns PENDING_CREATIVES for ``not has_creatives or not creatives_approved``,
        which is two different states wearing one name --

          * ``not has_creatives``     an auto-approved buy with nothing supplied yet.
                                      The adapter WAS contacted; the seller committed.
          * ``not creatives_approved`` a buy held on creative review. The hold returns
                                      before the adapter is ever reached; no commitment.

        A status-keyed rule must be wrong about one of them, whichever way it is set:
        including the member minted a commitment for a held buy that a later failure
        carried to its grave, and excluding it dropped the commitment an auto-approved
        buy had genuinely earned.

        Commitment is an EVENT, so it is recorded where it happens. The two writers
        that know are the synchronous create after the adapter returns, and the single
        post-adapter approval writer. Every other caller leaves the default and cannot
        mint one by accident -- fail-closed, because a false commitment instant is
        buyer-visible and write-once, while a missing one is corrected by the next
        genuine commit.

        Pinned contract: ``create-media-buy-response.json`` @ 3.1.1 arm0 types
        ``confirmed_at`` ["string","null"], lists it in ``required``, and describes it
        as "the moment the seller committed... May be null in deferred or
        manual-approval flows until seller commitment occurs" -- an event, in the
        spec's own words. Its one hard constraint is that a null value forbids
        ``status == "active"``, which holds: ACTIVE is only ever reached through a
        writer that commits.

        Graded by @T-UC-002-v31-success-revision-and-actions (the auto-approval arm,
        which requires a timestamp) and by the approval-route integration tests (the
        held arm, which requires NULL). Settles the question filed as #2116.
        """
        if media_buy.confirmed_at is not None:
            return False
        if not seller_committed:
            return False
        media_buy.confirmed_at = datetime.datetime.now(datetime.UTC)
        return True

    def update_status(
        self,
        media_buy_id: str,
        status: PersistedMediaBuyStatus,
        *,
        seller_committed: bool = False,
        approved_at: datetime.datetime | None = None,
        approved_by: str | None = None,
    ) -> MediaBuy | None:
        """Update the status of a media buy within this tenant.

        Returns the updated MediaBuy, or None if not found in this tenant.

        The status must be a member of the persisted vocabulary. Rejecting an
        unknown value HERE is what lets every reader stop guessing: the wire
        projection maps persisted statuses to protocol ones, and a value it has no
        row for would otherwise be reported as a generic serving state — publishing
        ``active`` for a state nobody defined, with no commitment instant to go with
        it, which the pinned response schema forbids. A closed vocabulary is enforced
        where values enter, not interpreted where they are read.
        """
        normalized = PersistedMediaBuyStatus.parse(status, media_buy_id=media_buy_id)
        media_buy = self.get_by_id(media_buy_id)
        if media_buy is None:
            return None
        # Store the canonical spelling: casing is not meaning, so it is normalized
        # once here rather than tolerated by every downstream reader.
        media_buy.status = normalized
        if approved_at is not None:
            media_buy.approved_at = approved_at
        if approved_by is not None:
            media_buy.approved_by = approved_by
        # Stamp before bumping: _bump_revision leaves revision holding a SQL
        # expression, and reading any attribute after that can trigger a refresh
        # mid-mutation. The stamp reads status and confirmed_at, so it goes first.
        self._stamp_confirmation_if_needed(media_buy, seller_committed=seller_committed)
        self._bump_revision(media_buy)
        self._session.flush()
        return media_buy

    def _lock_row_for_revision_check(
        self,
        media_buy_id: str,
        *,
        expected_revision: int,
        context: ContextObject | dict[str, Any] | None = None,
    ) -> MediaBuy | None:
        """Re-read the row under a row lock, bounded by a lock timeout.

        ``with_for_update()`` is what makes the comparison correct: it is the lock, and
        dropping it lets two racing transactions both read the same revision and both
        commit -- the lost update the spec's "atomically" clause forbids. The race test
        in tests/integration/test_media_buy_repository_writes.py grades exactly that.

        ``populate_existing=True`` states the requirement the comparison depends on: the
        row must come from THIS select, not from whatever the session already holds --
        and in this flow it does already hold one, because the caller verified ownership
        first. Measured on the pinned SQLAlchemy, ``with_for_update()`` alone already
        refreshes the identity-map row's columns, so this option changes no behaviour
        today; it is kept because the comparison's correctness should rest on something
        the query says rather than on a refresh the ORM is not asked for.

        The lock timeout keeps a contended request from pinning a connection behind
        whoever holds the row. Timing out means the current revision was never
        observed, which is a conflict the buyer can retry -- not a server fault.
        """
        # Postgres SET does not accept bind parameters; set_config(..., is_local=true)
        # does, and is likewise reverted when this transaction ends.
        self._session.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": f"{_REVISION_LOCK_TIMEOUT_MS}ms"},
        )
        statement = (
            select(MediaBuy)
            .where(
                MediaBuy.tenant_id == self._tenant_id,
                MediaBuy.media_buy_id == media_buy_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        try:
            return self._session.scalars(statement).first()
        except OperationalError as exc:
            if getattr(exc.orig, "pgcode", None) != _LOCK_NOT_AVAILABLE:
                raise
            raise AdCPRevisionConflictError.unobserved(
                media_buy_id=media_buy_id, expected=expected_revision, context=context
            ) from exc

    def assert_revision_matches(
        self,
        media_buy_id: str,
        *,
        expected_revision: int,
        context: ContextObject | dict[str, Any] | None = None,
    ) -> MediaBuy:
        """Compare the buyer's expected ``revision`` WITHOUT locking or advancing.

        This is the fail-fast half of the two-phase enforcement the pinned
        update-media-buy-request.json needs. It answers CONFLICT on a token that is
        already stale when the request arrives, which is what keeps a stale token from
        reaching the adapter at all: ``compare_and_set_revision`` cannot be used here
        because the lock it takes would not survive to the write (the update flow runs
        nested ``get_db_session()`` blocks in between, and they end this transaction),
        so taking it early would buy a guarantee that has expired by the time it is
        needed while still costing every caller a row lock.

        It is deliberately NOT sufficient on its own: a mutation landing after this
        returns is invisible to it. ``compare_and_set_revision``, run in the same
        transaction as the write, is what actually enforces the spec's "atomically"
        clause. See ``_update_media_buy_impl``.

        Raises:
            AdCPRevisionConflictError: the current revision differs from
                ``expected_revision``.
            AdCPInvariantViolationError: the row is gone.
        """
        media_buy = self.get_by_id(media_buy_id)
        if media_buy is None:
            raise AdCPInvariantViolationError(
                f"media buy {media_buy_id} (tenant {self._tenant_id}) disappeared between the "
                f"ownership check and the revision comparison",
                context=context,
            )
        if media_buy.revision != expected_revision:
            raise AdCPRevisionConflictError.mismatch(
                media_buy_id=media_buy_id,
                expected=expected_revision,
                current=media_buy.revision,
                context=context,
            )
        return media_buy

    def compare_and_set_revision(
        self,
        media_buy_id: str,
        *,
        expected_revision: int,
        seller_committed: bool = False,
        context: ContextObject | dict[str, Any] | None = None,
    ) -> MediaBuy:
        """Enforce the buyer's expected ``revision`` atomically with this transaction's write.

        The pinned update-media-buy-request.json requires that a supplied ``revision``
        be rejected with ``CONFLICT`` when it does not match, and that the comparison
        be enforced ATOMICALLY with the write. Locking the row is what supplies the
        second half: a Postgres row lock is held until the transaction ends, so once
        this returns, no other transaction can move the revision out from under the
        writes that follow in the same transaction.

        That last phrase is the whole constraint on WHERE this may be called. A caller
        that runs a nested ``get_db_session()`` block after this returns has ended the
        transaction and dropped the lock, and the comparison no longer covers anything.
        The update flow therefore calls this as LATE as it can -- after the adapter, at
        the last point before its first persistent write -- and uses
        ``assert_revision_matches`` for the early rejection instead.

        The advance is taken here rather than deferred, so the buyer's token is spent the
        moment it is honoured, and so this method can return the new value to a caller
        that may go on to write nothing at all. It is a PREPAYMENT: the next write to
        this buy in the same unit of work draws on it instead of advancing again, so
        honouring a token costs the buyer no extra revisions. See ``_bump_revision``.

        Raises:
            AdCPRevisionConflictError: the current revision differs from
                ``expected_revision``, or the row could not be locked in time.
            AdCPInvariantViolationError: the row is gone. Callers reach this method
                having already read the row (ownership is verified first), so a
                locked read that finds nothing means it was deleted mid-request.
        """
        media_buy = self._lock_row_for_revision_check(
            media_buy_id, expected_revision=expected_revision, context=context
        )
        if media_buy is None:
            raise AdCPInvariantViolationError(
                f"media buy {media_buy_id} (tenant {self._tenant_id}) disappeared between the "
                f"ownership check and the locked revision re-read",
                context=context,
            )
        if media_buy.revision != expected_revision:
            raise AdCPRevisionConflictError.mismatch(
                media_buy_id=media_buy_id,
                expected=expected_revision,
                current=media_buy.revision,
                context=context,
            )
        # Same ordering rule as update_status/update_fields: stamp (which reads
        # attributes) before the bump (which replaces one with a SQL expression).
        self._stamp_confirmation_if_needed(media_buy, seller_committed=seller_committed)
        self._bump_revision(media_buy)
        self._session.flush()
        # _bump_revision leaves ``revision`` holding a SQL expression, and a flush alone
        # does not resolve it. Callers here READ the new value (the update flow reports
        # it back to the buyer), so unlike the other write paths this one cannot leave
        # the window open -- refresh it into a real int before returning.
        self._session.refresh(media_buy, ["revision"])
        # Recorded AFTER the refresh, so the prepayment names a real int rather than the
        # expression, and so a later write can tell a surviving advance from a lost one.
        self._prepaid_revision_advance[media_buy_id] = media_buy.revision
        return media_buy

    def update_fields(self, media_buy_id: str, *, seller_committed: bool = False, **kwargs: Any) -> MediaBuy | None:
        """Update arbitrary fields on a media buy within this tenant.

        Only updates fields that are valid MediaBuy column attributes.
        Returns the updated MediaBuy, or None if not found in this tenant.
        Raises ValueError if any kwarg is not a valid MediaBuy attribute or
        if the caller attempts to update an immutable field (tenant_id,
        media_buy_id, created_at, revision, confirmed_at).
        """
        blocked = self._MEDIA_BUY_IMMUTABLE_FIELDS & kwargs.keys()
        if blocked:
            raise ValueError(f"Cannot update immutable field(s): {', '.join(sorted(blocked))}")
        # status is mutable here, so this door needs the same vocabulary check
        # update_status applies — otherwise the closed vocabulary is enforced on one
        # write path and bypassable on another.
        if "status" in kwargs:
            kwargs["status"] = PersistedMediaBuyStatus.parse(kwargs["status"], media_buy_id=media_buy_id)
        media_buy = self.get_by_id(media_buy_id)
        if media_buy is None:
            return None
        for key, value in kwargs.items():
            if not hasattr(media_buy, key):
                raise ValueError(f"MediaBuy has no attribute {key!r}")
            setattr(media_buy, key, value)
        # Same ordering rule as update_status: stamp (which reads attributes) before
        # the bump (which replaces one with a SQL expression).
        self._stamp_confirmation_if_needed(media_buy, seller_committed=seller_committed)
        self._bump_revision(media_buy)
        self._session.flush()
        return media_buy

    # ------------------------------------------------------------------
    # MediaPackage writes
    # ------------------------------------------------------------------

    def _bump_parent_revision(self, media_buy_id: str) -> None:
        """Fetch the parent buy and move its concurrency token.

        Package-level writes change what the buyer sees on the media buy, so they
        move its revision too: they persist the package directly on the session
        rather than going through update_status / update_fields, which would
        otherwise leave the parent's token stale.

        For the two package writers that hold only the package (update_package_config,
        update_package_fields). The two that already hold the parent row
        (create_package, create_packages_bulk) call ``_bump_revision`` directly rather
        than paying for a second lookup.

        Raises rather than skipping when the parent is missing: get_package joins
        through MediaBuy under the tenant filter, so a package that was found
        guarantees a parent that exists. Swallowing a miss here would leave the
        buyer's concurrency token stale with no signal.
        """
        self._bump_revision(self.get_by_id_or_raise(media_buy_id))

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
        self._bump_revision(media_buy)
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
        self._bump_parent_revision(media_buy_id)
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
        self._bump_parent_revision(media_buy_id)
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
        self._bump_revision(media_buy)
        self._session.flush()
        return packages

    # ------------------------------------------------------------------
    # Cross-tenant queries (for system-level schedulers)
    # ------------------------------------------------------------------

    @staticmethod
    def get_all_by_statuses(session: Session, statuses: Collection[PersistedMediaBuyStatus]) -> list[MediaBuy]:
        """Get media buys across ALL tenants filtered by status.

        This is a system-level query for schedulers that need to process
        media buys regardless of tenant. Not tenant-scoped.

        Takes the enum, not strings. A ``list[str]`` parameter let a caller spell the
        same set of states a second time, next to the enum set it already had, and the
        two drifted apart in silence — adding a member to one left the other behind.
        ``PersistedMediaBuyStatus`` is a ``StrEnum``, so members bind against the
        ``String`` column exactly as the bare strings did.
        """
        return list(session.scalars(select(MediaBuy).where(MediaBuy.status.in_(statuses))).all())
