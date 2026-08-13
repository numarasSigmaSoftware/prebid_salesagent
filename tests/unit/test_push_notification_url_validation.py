"""The buyer-callback validator must give the same verdict for both input shapes.

``validate_push_notification_config_url`` is reached from two entry points that hand it
different types for the same field: ``create_media_buy``'s wrappers deserialize JSON into
a dict of plain strings, while ``update_media_buy`` passes ``req.push_notification_config``
through as the typed model — whose ``url`` is a Pydantic ``AnyUrl``, not a ``str``.

The regression this pins: the validator type-guarded ``isinstance(url, str)`` before
coercing, which is correct for the dict path and rejected EVERY typed config on the update
path, safe URL or not. Every buyer updating a media buy with a callback got
``VALIDATION_ERROR`` on a perfectly good URL.

It shipped because the only test was a REJECTION test — "update rejects a private
callback" — and a rejection test passes just as well when everything is rejected. So this
grades BOTH directions on BOTH shapes: an accept case is what makes the reject case
meaningful.
"""

from __future__ import annotations

import pytest
from adcp.types import PushNotificationConfig

from src.core.exceptions import AdCPValidationError
from src.core.tools.media_buy_create import validate_push_notification_config_url

# A resolvable public host, so the accept case exercises the real SSRF check rather than
# failing on DNS and looking like a correct rejection.
PUBLIC_URL = "https://example.com/callback"
LOOPBACK_URL = "https://127.0.0.1/callback"


def _as_dict(url: str) -> dict[str, str]:
    """The shape create's wrappers produce (JSON -> plain strings)."""
    return {"url": url}


def _as_model(url: str) -> PushNotificationConfig:
    """The shape update passes through (``url`` is an ``AnyUrl``)."""
    return PushNotificationConfig(url=url)


@pytest.mark.parametrize("build", [_as_dict, _as_model], ids=["dict", "typed-model"])
def test_public_callback_is_accepted_on_both_input_shapes(build):
    """A safe callback must be accepted however it arrives.

    This is the case the original rejection-only test could not see.
    """
    assert validate_push_notification_config_url(build(PUBLIC_URL)) == PUBLIC_URL


@pytest.mark.parametrize("build", [_as_dict, _as_model], ids=["dict", "typed-model"])
def test_loopback_callback_is_rejected_on_both_input_shapes(build):
    """An unsafe callback must be refused however it arrives."""
    with pytest.raises(AdCPValidationError) as exc_info:
        validate_push_notification_config_url(build(LOOPBACK_URL))
    assert exc_info.value.field == "push_notification_config.url"


def test_both_shapes_agree_url_for_url():
    """The two entry points cannot diverge on the same URL.

    Asserted as an equality between the two verdicts rather than two independent
    expectations, so a future change that loosens or tightens one path alone reddens here
    even if both individual cases still look reasonable on their own.
    """
    for url in (PUBLIC_URL, LOOPBACK_URL):

        def verdict(cfg):
            try:
                return ("accepted", validate_push_notification_config_url(cfg))
            except AdCPValidationError:
                return ("rejected", None)

        assert verdict(_as_dict(url)) == verdict(_as_model(url)), (
            f"create and update disagree about {url!r} — the same buyer-supplied callback "
            "must get the same answer from both entry points"
        )
