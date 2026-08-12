"""CI guard: assert the adcp SDK pin targets the expected AdCP spec version."""

import adcp

EXPECTED_SPEC_VERSION = "3.1.1"


def test_adcp_spec_version_matches_pin() -> None:
    """Verify SDK pin targets the spec version this codebase expects.

    Failure here means the adcp Python SDK pin in pyproject.toml has shifted
    to a version that targets a different AdCP spec version. Either revert
    the pin or follow docs/adcp-spec-version.md to update
    EXPECTED_SPEC_VERSION and the related references it lists.
    """
    actual = adcp.get_adcp_spec_version()
    assert actual == EXPECTED_SPEC_VERSION, (
        f"adcp SDK targets spec {actual}, but this codebase expects "
        f"{EXPECTED_SPEC_VERSION}. See docs/adcp-spec-version.md for "
        f"reconciliation steps."
    )


class TestOutboundStampsAreReleasePrecision:
    """What we stamp outbound must be a value we would ourselves accept inbound.

    `get_adcp_spec_version()` is PATCH precision ("3.1.1"); the wire envelope
    (`core/version-envelope.json`, v3.1.1) is release precision (MAJOR.MINOR).
    Three outbound sites stamped the SDK pin, so this agent emitted a version
    string its OWN `_RELEASE_PIN_RE` rejects — a buyer echoing our
    `adcp_version` back at us would have been answered VERSION_UNSUPPORTED.
    """

    def test_wire_version_is_accepted_by_our_own_release_pin_parser(self):
        from src.core.adcp_version import _parse_release_pin, wire_adcp_version

        stamped = wire_adcp_version()

        assert _parse_release_pin(stamped) is not None, (
            f"we stamp {stamped!r} outbound but our own inbound parser rejects it"
        )

    def test_wire_version_is_one_we_advertise(self):
        from src.core.adcp_version import supported_adcp_versions, wire_adcp_version

        assert wire_adcp_version() in supported_adcp_versions()

    def test_the_sdk_pin_itself_would_not_satisfy_this(self):
        """Pins the reason the helper exists, so nobody 'simplifies' it back.

        If the SDK pin ever became release-precision this test would fail
        loudly rather than let the distinction quietly stop mattering.
        """
        from adcp import get_adcp_spec_version

        from src.core.adcp_version import _parse_release_pin

        assert _parse_release_pin(get_adcp_spec_version()) is None, (
            "the SDK spec pin is now release-precision — re-evaluate wire_adcp_version()"
        )
