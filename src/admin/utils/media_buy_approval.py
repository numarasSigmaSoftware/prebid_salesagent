"""Shared typed result construction for approved media-buy workflows."""

from __future__ import annotations

from typing import Any

from src.admin.utils.helpers import echo_context
from src.core.database.repositories.media_buy import MediaBuyRepository
from src.core.schemas import CreateMediaBuySuccess, Package


def build_approved_media_buy_result(
    repository: MediaBuyRepository,
    media_buy_id: str,
    request_data: dict[str, Any],
) -> CreateMediaBuySuccess:
    """Build the canonical create-media-buy success used by every admin route.

    ``confirmed_at``/``revision`` carry no field default (the repository owns both
    columns), so this reads what the row actually holds rather than fabricating a
    value — the same rule ``sync_success()`` documents.
    """
    media_buy = repository.get_by_id_or_raise(media_buy_id)
    packages = repository.get_packages(media_buy_id)
    return CreateMediaBuySuccess.sync_success(
        media_buy_id=media_buy_id,
        packages=[Package(package_id=package.package_id) for package in packages],
        confirmed_at=media_buy.confirmed_at,
        revision=media_buy.revision,
        context=echo_context(request_data),
    )
