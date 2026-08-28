"""Every replay family echoes the current retry's application context."""

from src.core.schemas import (
    CreateMediaBuySuccess,
    SyncAccountsResponse,
    SyncCreativesResponse,
    UpdateMediaBuySuccess,
)
from src.core.tools.accounts import _decode_sync_accounts_replay
from src.core.tools.creatives._sync import _decode_sync_creatives_replay
from src.core.tools.media_buy_create import _replay_cached_success
from src.core.tools.media_buy_update import _decode_update_media_buy_replay

_ORIGINAL = {"correlation_id": "original"}
_RETRY = {"correlation_id": "retry"}


def test_create_replay_echoes_retry_context() -> None:
    cached = CreateMediaBuySuccess.carrier(media_buy_id="mb-1", packages=[], context=_ORIGINAL)

    replay = _replay_cached_success(
        {"status": "completed", "response": cached.model_dump(mode="json")},
        context=_RETRY,
    )

    assert replay is not None
    assert replay.replayed is True
    assert replay.response.context == _RETRY


def test_update_replay_echoes_retry_context() -> None:
    cached = UpdateMediaBuySuccess.carrier(media_buy_id="mb-1", affected_packages=[], context=_ORIGINAL)

    replay = _decode_update_media_buy_replay(
        {"status": "completed", "response": cached.model_dump(mode="json")},
        context=_RETRY,
    )

    assert replay is not None
    assert replay.replayed is True
    assert replay.response.context == _RETRY


def test_sync_accounts_replay_echoes_retry_context() -> None:
    cached = SyncAccountsResponse(accounts=[], context=_ORIGINAL)

    replay = _decode_sync_accounts_replay(
        {"status": "completed", "response": cached.model_dump(mode="json")},
        context=_RETRY,
    )

    assert replay is not None
    assert replay.replayed is True
    assert replay.context == _RETRY


def test_sync_creatives_replay_echoes_retry_context() -> None:
    cached = SyncCreativesResponse(creatives=[], context=_ORIGINAL)

    replay = _decode_sync_creatives_replay(
        {"status": "completed", "response": cached.model_dump(mode="json")},
        context=_RETRY,
    )

    assert replay is not None
    assert replay.replayed is True
    assert replay.context == _RETRY
