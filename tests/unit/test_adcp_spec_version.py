"""CI guard: assert the adcp SDK pin targets the expected AdCP spec version."""

import adcp

EXPECTED_SPEC_VERSION = "3.1.1"
EXPECTED_SCHEMA_SOURCE_SHA = "467fd93d77112baf9e094e18980119edcd3a4d07"


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


def test_pinned_schema_refresh_source_matches_spec_release() -> None:
    """Keep the vendored schema refresh source on the declared v3.1.1 release."""
    from tests.fixtures.adcp_schemas_pinned._refresh import PINNED_SHA

    assert PINNED_SHA == EXPECTED_SCHEMA_SOURCE_SHA, (
        f"Pinned schemas claim AdCP {EXPECTED_SPEC_VERSION} but refresh from "
        f"{PINNED_SHA}; expected the immutable v{EXPECTED_SPEC_VERSION} release "
        f"{EXPECTED_SCHEMA_SOURCE_SHA}."
    )
