# AdCP Spec Version

Prebid Sales Agent targets **AdCP spec version 3.1.1**.

## Verifying the current target

```python
import adcp
adcp.get_adcp_spec_version()  # "3.1.1"
adcp.get_adcp_sdk_version()   # "6.6.0"
```

## Why this version

The `adcp` Python SDK is pinned in `pyproject.toml` to `==6.6.0`. SDK 6.6.0
is code-generated from AdCP spec 3.1.1 and ships Pydantic models that
encode that spec version's request/response shapes.

The SDK-to-spec mapping (verified via each wheel's bundled `ADCP_VERSION`
file):

| adcp SDK release | AdCP spec |
|---|---|
| 4.3.x | 3.0.1 |
| 4.4.x | 3.0.5 |
| 4.5.x – 4.6.x | 3.0.5 |
| 5.0.x – 5.6.x | 3.0.7 |
| 5.7.x | 3.1.0-beta.3 |
| 6.x (stable) | 3.1.1 |

To check what spec version any installed SDK release targets:

```bash
uv run python -c "import adcp; print(adcp.get_adcp_spec_version())"
```

## CI guard

`tests/unit/test_adcp_spec_version.py` asserts the installed SDK targets
`3.1.1`. A pin shift will fail this test, forcing a deliberate update
across `pyproject.toml`, the test's `EXPECTED_SPEC_VERSION` constant, and
this document.

## Behavior target vs SDK pin

The SDK **pin** (3.1.1) fixes the request/response *type shapes* we
build against. It does not replace the authoritative prose and compliance
storyboards for protocol behavior. For example, the published **3.1.1**
`pending_creatives_to_start.yaml` storyboard grades `media_buy_status`
  as `field_value` (the DOMAIN status) and the top-level `status` as
  `field_value` `'completed'` (the PROTOCOL `TaskStatus`, protocol envelope).
  The two are DIFFERENT namespaces and are NOT identical.

Our wire already implements the divergent (target GA) model:
`TaskResultEnvelope._serialize` sets the top-level `status` to the protocol
`TaskStatus`, while the domain status survives under `media_buy_status`
(`src/core/schemas/_base.py` `_mirror_media_buy_status`). The dual-emit
validator only backfills the deprecated **body** `status` from the domain
`media_buy_status` for the deprecation window; it does not touch the wire
top-level `status`.

**Historical SDK type defect (SDK not authoritative):** adcp 5.7 typed the response
`status` as `MediaBuyStatus | None`, but the wire top-level `status` carries a
protocol `TaskStatus` (`submitted` / `completed`). This is fine because that
protocol value lives on `TaskResultEnvelope.status` (typed `str`), never on the
SDK-typed body field. Grounding for the divergent behavior is the value-pinned
`media_buy_status` assertions in
`tests/bdd/features/BR-UC-002-media-buy-status-dual-emit.feature` and the
`then_dual_emit_media_buy_status` step in
`tests/bdd/steps/domain/uc002_create_media_buy.py` (see PR #1417).
`tests/unit/test_adcp_spec_version.py` only guards the SDK pin, not this behavior.

## Authentication error classification

The immutable AdCP **v3.1.1** source is tag `v3.1.1`, commit
`467fd93d77112baf9e094e18980119edcd3a4d07`. Its
`static/schemas/source/enums/error-code.json` metadata requires sellers to
return:

- `AUTH_MISSING` when no standard `Authorization` header was included
  (`correctable`: provide credentials and retry).
- `AUTH_INVALID` when an `Authorization` header was present but rejected
  (`terminal`, except the spec's one-time OAuth refresh allowance).

Prebid Sales Agent applies that split at every A2A, MCP, and REST wire
boundary. The legacy `x-adcp-auth` extension remains accepted as an input
channel, but it does not change the standard header-presence classifier.
Direct `_impl` helpers retain deprecated `AUTH_REQUIRED` only where no wire
credential-presence information exists.

This behavior is **ungraded** by the official conformance storyboards:
`dist/compliance/3.1.1/universal/error-compliance.yaml` contains no
`AUTH_*` scenario. The normative enum mandate is covered by the repository's
cross-transport wire tests and the pinned fixture at
`tests/fixtures/adcp_schemas_pinned/enums/error-code.json`.

## Wire negotiation

AdCP wire values for `adcp_version` are release-precision (`"3.0"`,
`"3.1"`). The SDK accepts patch-precision input for backwards
compatibility but normalizes to release-precision on the wire.

## Bumping the spec version

A spec version bump is a deliberate change with downstream impact:

1. Read the AdCP spec changelog for the target version.
2. Update `pyproject.toml` SDK pin to a release that targets the new spec
   version (see mapping above).
3. Run `uv lock --upgrade-package adcp`.
4. Update `EXPECTED_SPEC_VERSION` in `tests/unit/test_adcp_spec_version.py`.
5. Update this document.
6. Run `make quality` and address Pydantic field/type changes.
7. Re-verify integration and BDD test coverage.

## Related files

- `pyproject.toml` — SDK pin
- `tests/unit/test_adcp_spec_version.py` — CI guard
- `docs/adcp-spec-version.md` — this document
