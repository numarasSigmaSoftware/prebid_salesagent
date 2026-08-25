# Local compiler overlays for derivative runner/fixture defects.
#
# The boundary overlay replaces a fragile hand-counted over-max value with a
# token the bound Given step expands to exactly 256 valid-pattern characters.
# scripts/compile_bdd.py applies this replacement in both wholesale and merge
# modes. (The earlier supported=false replay reconciliation was removed when
# create_media_buy replay was restored — the upstream replay scenario now
# grades production directly.)

Feature: BR-UC-002 local capability reconciliation

  @T-UC-002-v31-idempotency-in-flight @v31 @idempotency-key @error-details @post-f2 @post-f3 @ext-w
  Scenario: v3.1 idempotency_key matching an in-flight request rejects with IDEMPOTENCY_IN_FLIGHT
    Given a create_media_buy request with idempotency_key "buy-2026-q1-inflight-001"
    And a prior request for the same (seller, account, idempotency_key) pair is still in flight
    When the Buyer Agent sends the create_media_buy request
    Then the error recovery should be "transient"
    And the error code should be "IDEMPOTENCY_IN_FLIGHT"
    And the error should include "retry_after" field
    And the error should include "suggestion" field
    And no new media buy should have been created
    # LOCAL DIVERGENCE (mirror upstream): generated as "Then the response
    # should indicate a terminal failure" -- that phrasing has no step
    # definition (then_error.py has no "indicate a transient/terminal
    # failure" binding for this recovery class other than the bound
    # "terminal failure" step, which is also the wrong recovery value here),
    # so this overlay uses the bound `the error recovery should be
    # "{recovery}"` step (tests/bdd/steps/generic/then_error.py:413,
    # wire-first via assert_wire_error) instead. The pinned spec's
    # dist/schemas/3.1.1/enums/error-code.json recovery-classification block
    # gives "IDEMPOTENCY_IN_FLIGHT" recovery "transient" (verbatim: "recovery":
    # "transient", ref v3.1.1) -- production (AdCPIdempotencyInFlightError's
    # _default_recovery = "transient") and TestInFlightWireMatrix already
    # agree with the spec. The scenario's tag is absent from
    # _UC002_IDEMPOTENCY_WIRED and its Given step has no definition, so it
    # still auto-xfails; wiring it up is separate harness work, out of scope.
    # BR-RULE-211 INV-4: in-flight key MAY reject with IDEMPOTENCY_IN_FLIGHT; buyer MUST NOT mint a fresh key
    # @source repo=adcp ref=v3.1-04f59d2d5 commit=04f59d2d5 path=static/schemas/source/media-buy/create-media-buy-request.json

  @T-UC-002-v31-idempotency-pattern-invalid @v31 @idempotency-key @validation @post-f2 @ext-w
  Scenario Outline: v3.1 idempotency_key violates length/pattern constraints
    Given a create_media_buy request with idempotency_key "<value>"
    And the account "acc-001" exists and is active
    When the Buyer Agent sends the create_media_buy request
    Then the response should indicate a validation error
    And the error should reference idempotency_key constraint "<violation>"
    And the error should include "suggestion" field
    # v3.1: idempotency_key pattern ^[A-Za-z0-9_.:-]{16,255}$
    # @source repo=adcp ref=v3.1-04f59d2d5 commit=04f59d2d5 path=static/schemas/source/media-buy/create-media-buy-request.json

    Examples:
      | value                                      | violation                        |
      | short                                      | minLength 16 violated            |
      | key with spaces in it that is long enough | pattern [A-Za-z0-9_.:-] violated |
      | key/with/slashes/that/is/also/long/enough | pattern [A-Za-z0-9_.:-] violated |
      | <256 chars>                                | maxLength 255 violated           |
