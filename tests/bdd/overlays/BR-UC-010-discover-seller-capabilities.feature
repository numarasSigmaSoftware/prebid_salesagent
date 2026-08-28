# Local compiler overlay for a step-shadowing defect (#1941).
#
# The generated scenario used the literal sentence "the error details should
# include supported_versions as a non-empty array", which is ALSO matched by
# the generic `the error details should include {key} {value}` parser in
# tests/bdd/steps/generic/then_error.py. Two fixtures registering for one
# sentence means pytest-bdd's registration-order resolution silently drops
# one of them (test_architecture_bdd_no_shadowed_steps.py::
# test_no_domain_exact_text_shadows_generic_parser) — here the generic step
# would have compared the wire supported_versions array against the literal
# string "as a non-empty array" instead of running the real domain assertion
# in tests/bdd/steps/domain/uc010_version_negotiation.py
# (then_details_supported_versions_nonempty). This overlay renames the
# sentence to "the error details should carry a non-empty supported_versions
# array", matching the step definition's rename, so the wording no longer
# collides with the generic parser's shape.

Feature: BR-UC-010 local step-shadowing reconciliation

  @T-UC-010-v31-version-unsupported @v31 @extension @ext-f @error @post-f2 @post-f4 @partition
  Scenario: version-unsupported — VERSION_UNSUPPORTED error carries authoritative supported_versions
    Given a tenant is resolvable from the request context
    And the seller speaks adcp release-precision versions "3.0", "3.1"
    When the Buyer Agent calls get_adcp_capabilities MCP tool with adcp_version "4.0"
    Then the response should be a VERSION_UNSUPPORTED error
    And the error code should be "VERSION_UNSUPPORTED"
    And the error details should carry a non-empty supported_versions array
    And each supported_versions entry should match pattern "^\\d+\\.\\d+(-[a-zA-Z0-9.-]+)?$"
    And the error details may include supported_majors as a deprecated array of integers
    And the error details may include build_version as an advisory semver string
    And the Buyer Agent may re-pin to a value from supported_versions and retry without a second discovery round-trip
    And the error should include "suggestion" field advising the Buyer to re-pin to a supported_versions entry
    # v3.1 Phase 1.5 wave A: error-details/version-unsupported.json
    # PRE-BIZ11 / BR-19: supported_versions REQUIRED with minItems:1; supported_majors DEPRECATED; build_version advisory only
    # POST-F2: Buyer knows the specific error code
    # POST-F4: Buyer can re-pin and retry without another discovery round-trip
    # @source repo=adcp ref=v3.1-04f59d2d5 commit=04f59d2d5 path=static/schemas/source/protocol/get-adcp-capabilities-response.json
