# PR #1546 — AdCP 3.1.1 protocol grounding

This note records the authoritative AdCP contract used for the protocol-facing
changes in PR #1546. The repository pins `adcp==6.6.0`, which maps to AdCP
3.1.1. All upstream paths below are at the `adcontextprotocol/adcp` tag
`v3.1.1`.

AdCP 3.1.1 is a patch of the 3.1 release, so its schemas and compliance
storyboards are published under `3.1.1`, while the unchanged 3.1 prose shipped
at that tag remains under `dist/docs/3.1.0`.

## Version negotiation and context echo

Authoritative sources:

- `dist/docs/3.1.0/protocol/get_adcp_capabilities.mdx`, section
  **Version Negotiation**
- `dist/docs/3.1.0/reference/versioning.mdx`
- `dist/schemas/3.1.1/core/version-envelope.json`
- `dist/schemas/3.1.1/error-details/version-unsupported.json`
- `dist/schemas/3.1.1/protocol/get-adcp-capabilities-request.json`
- `dist/schemas/3.1.1/protocol/get-adcp-capabilities-response.json`
- `dist/schemas/3.1.1/core/context.json` (an opaque
  `additionalProperties: true` object)
- `dist/docs/3.1.0/building/by-layer/L2/context-sessions.mdx`, section
  **Normative echo contract**, especially rules 2 and 5 (echo application
  context on success and error without removing or retyping fields)

Grading status:

- `dist/compliance/3.1.1/universal/version-negotiation.yaml` grades
  `adcp.supported_versions`, response-envelope `adcp_version`, and an unchanged
  request `context` echo. In 3.1.1 the release-precision advertisement and
  envelope echo are advisory checks; the context-presence and exact-value checks
  are not marked advisory.
- `dist/compliance/3.1.1/universal/error-compliance.yaml`, phase
  `version_negotiation`, grades `VERSION_UNSUPPORTED` for an unsupported major.
  Its release-precision sibling is advisory in 3.1; both paths grade unchanged
  context echo.
- The local cross-major, below-minimum same-major, and unmatched-prerelease
  resolution cases in `BR-UC-010-version-negotiation.feature` are companion
  coverage and are not separate published 3.1.1 storyboard steps.

Decision for this PR: real MCP, A2A, and REST scenarios treat the captured wire
response as the sole context-echo oracle. Only the non-wire IMPL test path may
inspect a typed response directly. Serialization distinguishes omission from
an explicitly supplied JSON null: generated SDK defaults may omit unset schema
fields, but must not delete null-valued keys inside the buyer-owned opaque
context. The same shared serializer is used for success responses and typed
error envelopes.

The two version fields have deliberately separate sources. AdCP
`supported_versions` is derived from the SDK's pinned spec release (`3.1.1` →
wire release `3.1`). Advisory `build_version` identifies the Sales Agent
deployment lineage for incident triage and comes from the seller package
version (`src.core.version.get_version()`); it is not another spelling of the
AdCP spec release and is never a negotiation candidate. The same seller build
identifier is emitted in capabilities and `VERSION_UNSUPPORTED` details.

## Capability protocol filtering

Authoritative sources:

- `dist/schemas/3.1.1/protocol/get-adcp-capabilities-request.json`, property
  `protocols` (the five-value enum and `minItems: 1`)
- `dist/schemas/3.1.1/protocol/get-adcp-capabilities-response.json`, property
  `supported_protocols`
- `dist/compliance/3.1.1/universal/capability-discovery.yaml`, step
  `get_capabilities_filtered`

The published filtered-discovery step sends `protocols: ["media_buy"]` and
expects a schema-valid filtered response with unchanged context — its stated
expectation is "the same structure but only the requested domain details". The
local three-transport companion runs that request over MCP, A2A, and REST and
asserts the real wire. Unknown enum values and an empty array are
schema-grounded `VALIDATION_ERROR` cases.

The filter narrows the response's per-domain capability DETAIL sections; it does
not narrow `supported_protocols`. That field is the agent's own declaration —
the response schema describes each listed value as committing the agent "to pass
the baseline compliance storyboard" for that protocol — so it reports what this
agent implements, independent of what the buyer asked about.

A valid but unsupported-only filter (`["signals"]` against this media_buy-only
seller) is therefore an ordinary success: the true declaration, with no detail
sections. An earlier revision rejected it with `VALIDATION_ERROR`, reasoning
that `supported_protocols` has `minItems: 1` and so could not represent the
empty intersection. That reasoning only holds if the field is filtered in the
first place, and it put a schema-valid request on an error path that
`error-handling.mdx` scopes to schema violations.

## Authentication-before-version precedence

No pinned AdCP 3.1.1 prose, schema, or compliance step was identified that
mandates whether authentication or version negotiation must win when both are
invalid. This PR authenticates first as a local, ungraded non-disclosure
policy: an unauthenticated caller cannot use version errors to probe seller
capabilities. The UC-011 companion proves that precedence on the real wire; it
is not presented as an upstream conformance requirement.

## Required idempotency keys and the seller capability

Authoritative sources:

- `dist/docs/3.1.0/building/by-layer/L1/security.mdx`, section
  **Request Safety / Idempotency**, normative seller rules 1–10
- `dist/schemas/3.1.1/account/sync-accounts-request.json`
- `dist/schemas/3.1.1/creative/sync-creatives-request.json`
- `dist/schemas/3.1.1/media-buy/create-media-buy-request.json`
- `dist/schemas/3.1.1/media-buy/update-media-buy-request.json`
- `dist/schemas/3.1.1/protocol/get-adcp-capabilities-response.json`
- `dist/compliance/3.1.1/universal/read-tool-idempotency.yaml`
- `dist/compliance/3.1.1/universal/idempotency.yaml`
- `dist/docs/3.1.0/protocol/get_adcp_capabilities.mdx`, section
  **adcp / idempotency**

The sync-accounts, sync-creatives, create-media-buy, and update-media-buy
request schemas require `idempotency_key`; the field is a string of 16–255 characters matching
`^[A-Za-z0-9_.:-]{16,255}$`.

The capabilities schema is a discriminated union. `supported: true` requires
`replay_ttl_seconds`; `supported: false` means the seller does not deduplicate
retries and requires `replay_ttl_seconds` and `in_flight_max_seconds` to be
absent.

The 3.1.1 `read-tool-idempotency` storyboard says the every-request envelope
also applies to reads. Its `read_requests_accept_idempotency_key` phase grades
valid supplied keys on `get_adcp_capabilities`, `get_products`, `list_accounts`,
`list_creative_formats`, and `list_creatives`. Its
`omitted_key_grace_handled` branch explicitly permits either acceptance or
rejection when a read omits the key during 3.1; this seller takes the
compatibility-accept branch. The local ingress registry applies the same
behavior to all eight standard reads registered on MCP, including
`get_media_buys`, `get_media_buy_delivery`, and `list_tasks`. A2A and REST apply
the same validator to the subset of those operations that each transport
actually exposes; `list_tasks` remains intentionally MCP-only as documented in
`docs/development/a2a-mcp-agent-flows.md`, so this PR makes no A2A/REST
`list_tasks` claim. Rejecting a malformed *supplied* read key before stripping
it is a local, ungraded consistency rule using the same 16–255 character
constraint.

## Pause-on-create compatibility

Authoritative source: `dist/schemas/3.1.1/media-buy/create-media-buy-request.json`,
property `paused`. The field is accepted by the request schema, but no 3.1.1
compliance storyboard grades provider-side pause-on-create behavior. Until every
advertised adapter can create a campaign paused without an unreconciled second
mutation, this seller rejects `paused=true` with `UNSUPPORTED_FEATURE` and
an actionable `update_media_buy` suggestion instead of silently ignoring it.
`paused=false` remains the ordinary create behavior. This is an ungraded,
capability-truthfulness policy.

## Webhook timing and lifetime delivery

Authoritative sources:

- `dist/docs/3.1.0/building/by-layer/L1/webhooks.mdx`, section
  **When webhooks fire**: callbacks report changes after the initial response;
  a synchronous terminal response does not synthesize a duplicate callback.
- `dist/schemas/3.1.1/media-buy/get-media-buy-delivery-request.json`,
  `start_date`/`end_date`: omitting both requests campaign-lifetime data.

Native A2A registrations are persisted with the initial task but first emit only
after a later durable transition. Omitted delivery dates derive the selected
campaigns' inclusive flight lifetime rather than a rolling 30-day window.

### Durable universal replay and failure release

The agent-wide capability now declares `supported: true`,
`replay_ttl_seconds: 86400`, and `in_flight_max_seconds: 300`.

- Every supplied read key on every transport-exposed standard read is reserved
  before execution, stores the canonical typed response durably, and returns
  that immutable response with `replayed: true`. Read-key omission remains
  accepted only under the explicit 3.1.x grace. Anonymous public reads use
  `principal_id=NULL` inside the resolved tenant/account scope.
- `create_media_buy`, `update_media_buy`, `sync_accounts`, and
  `sync_creatives` reserve the
  `(tenant, principal, account, idempotency_key)` tuple before work begins.
- A live identical retry returns `IDEMPOTENCY_IN_FLIGHT`; a changed canonical
  payload or cross-tool reuse returns `IDEMPOTENCY_CONFLICT`; a completed
  identical retry returns the immutable original response with
  `replayed: true`.
- Reservation takeover rotates the attempt ID as a fencing token, so a stale
  worker cannot complete or release its successor's claim.
- Every execution or completion failure releases the attempt, as required by
  rules 3 and 9. Consequential downstream evidence is stored in an independent
  claim row, so releasing the buyer-facing attempt never erases the fact that a
  provider invocation may have occurred.
- Read and write insertion/active ceilings are counted independently. The
  legacy single-budget environment settings remain fallbacks for deployments
  that have not set the split controls.

The canonical hash is computed from the pre-normalization wire payload on MCP,
A2A, and REST. Per-scope admission limits apply before inserting a new
reservation. Context is excluded from the canonical hash but is overlaid from
the current request on replay, preserving the buyer-opaque echo contract
without mutating the cached payload.

### Rule 10 downstream reconciliation runbook

Rule 10 is reviewer-graded by the pinned spec. This implementation uses
write-claim-before-invoke:

1. The idempotency reservation and deterministic downstream-operation claim are
   committed together.
2. A PostgreSQL advisory transaction lock serializes the complete
   reconcile/CAS/provider-call sequence for the deterministic downstream
   request ID. A tenant-scoped compare-and-swap then moves a claim from
   `planned` or `not_applied` to `invoked`; only the fenced worker may call.
3. The seller-derived `downstream_request_id` is exposed to the adapter. The
   buyer's raw key is never forwarded or logged.
4. On retry, `APPLIED` reconstructs the stored/provider response,
   `NOT_APPLIED` permits exactly one invocation using the same request ID, and
   `UNKNOWN` is persisted and fails closed without another mutation.

Provider behavior:

- **GAM:** create orders carry the complete deterministic request ID in a
  queryable order-name suffix (plus a 31-bit `externalOrderId` compatibility
  marker); reconciliation queries the full suffix. Absence proves
  `NOT_APPLIED`. Presence proves that the order was accepted but cannot prove
  that every line item completed, so it remains `UNKNOWN` and fails closed.
  Update actions lack a safe per-operation marker and are rejected before
  provider invocation while idempotency reconciliation is required.
- **Mock:** the deterministic request ID is the exact operation key and maps to
  the recorded typed result, so all three outcomes are testable without
  inference.
- **Broadstreet and Kevel:** their documented APIs do not expose a native
  idempotency key or exact queryable operation marker for these calls. Keyed
  consequential mutations are rejected before provider invocation.
- **Triton:** the official API exposes external IDs, but this repository's
  current legacy endpoint contract does not yet use that API shape. Until that
  adapter is migrated, keyed consequential mutations are rejected before
  provider invocation.
- **Xandr:** explicitly unsupported before invocation because the adapter has
  no implemented exact reconciliation contract.

This runbook deliberately does not infer causation from a provider resource
whose current status or budget happens to match the requested value. That
"best-effort response inspection" is the third pattern rule 10 expressly
forbids. The cost of unavailable provider markers is a transient fail-closed
result requiring operator reconciliation, never a duplicate provider mutation.

### Generated UC-002 status

The upstream replay scenario `T-UC-002-v31-idempotency-replay` is LIVE in the
generated UC-002 feature — it grades production replay on a2a/mcp/rest as plain
PASS (original `media_buy_id` returned, `replayed: true`, adapter not
re-invoked, persisted set byte-stable), so no local overlay reconciles it. The
one remaining local overlay
(`tests/bdd/overlays/BR-UC-002-create-media-buy.feature`) is the boundary
fixture only: it replaces the upstream hand-counted over-max key with the
declarative `<256 chars>` token, which the bound Given step expands to exactly
256 valid-pattern characters so the maxLength boundary cannot silently drift.
`compile_bdd.py` applies exact-ID overlays in both `--all` and scenario-merge
modes, records provenance, and fails if the target ID disappears; unit coverage
grades both compiler paths.

The upstream supported-true phases this seller does NOT yet implement
(in-flight tracking, expired-window, canonical-comparison, conflict-details)
remain visible in the generated feature but unwired — tracked for the
reservation-subsystem rebuild (#1683). The local applicability guard asserts
the live discriminant is `IdempotencyUnsupported(supported=False)` — NOT
`supported: true`, which this note previously described. That inversion
mattered more here than in the source docstrings: this file is the
Spec-Grounding-Gate artifact, so it is what a reviewer reads to check the
claim against the spec, and it described the guard as asserting the opposite
of what it asserts (see `capabilities.py` and
`test_idempotency_capability_applicability.py`). The guard also pins that the
unimplemented phases stay visible-not-claimed.

## Outbound adcp_version precision (agent card, webhook payloads)

Authoritative source: `dist/schemas/3.1.1/core/version-envelope.json` (pinned
3.1.1, derived from `adcp.get_adcp_spec_version()` = 3.1.1 via `adcp==6.6.0`).

The wire `adcp_version` is RELEASE precision (MAJOR.MINOR, optional
prerelease). `get_adcp_spec_version()` returns PATCH precision ("3.1.1"), so
three outbound sites were stamping a value this agent's own inbound parser
(`_RELEASE_PIN_RE`, src/core/adcp_version.py) rejects: a buyer echoing our
advertised version back would be answered VERSION_UNSUPPORTED. Fixed by
`wire_adcp_version()`, which returns the highest ADVERTISED release — the same
list negotiation reads — at the delivery-webhook payload and the A2A
agent-card extension's `params.adcp_version`.

Graded: `tests/unit/test_adcp_spec_version.py::TestOutboundStampsAreReleasePrecision`
plus the payload assertion in `test_webhook_delivery_service.py`, which checks
against the advertisement and the real parser rather than against
`wire_adcp_version()` so both sides cannot drift together.

**The agent-card extension URI is deliberately NOT changed, and is not
spec-grounded.** An earlier revision of the code comment justified holding it
at patch precision by claiming it is "a schema PATH which exists at patch
precision". That is false: `dist/schemas/3.1.1/protocols` returns 404 and
`adcp-extension.json` was removed upstream in v3. The URI is a legacy
extension IDENTIFIER — an opaque match key for existing clients — held at its
historical value. It also diverges from the identifier the pinned guide tells
clients to match on; correcting that is wire-affecting and tracked separately.
Ungraded by any 3.1.1 storyboard step.

Storyboard: no `dist/compliance/3.1.1` step grades outbound version precision
on the agent card; the negotiation-error path (`supported_versions`) is graded
and is covered by the UC-010 scenarios.

## Update-media-buy revision

Authoritative source:

- `dist/schemas/3.1.1/media-buy/update-media-buy-request.json`, property
  `revision`

When supplied, `revision` is an optimistic-concurrency precondition. The schema
requires the seller to compare it atomically with the write and return
`CONFLICT` when it does not equal the current revision.

Grading status: no dedicated revision/optimistic-concurrency storyboard is
published under `dist/compliance/3.1.1`; this repository's BR-UC-003 scenarios
are local schema-derived coverage, not a claim that the upstream compliance
runner grades this behavior.

Decision for this PR: each media-buy row persists a revision beginning at 1.
The update transaction locks the tenant-scoped row, compares a supplied
revision before any provider mutation, returns `CONFLICT` with the current
revision on mismatch, and increments exactly once when an update is durably
applied. Omission retains last-write-wins compatibility and successful updates
still advance the stored revision.

Omission remains valid and proceeds. Explicit JSON `null` is REJECTED as
schema-invalid (`INVALID_REQUEST`) — it is not treated as a spelling of
omission. Grounding: the pinned
`dist/schemas/3.1.1/media-buy/update-media-buy-request.json` defines `revision`
as `{"type": "integer", "minimum": 1}`, with no `anyOf`-null member, so a JSON
`null` violates the type constraint outright; it is schema-invalid, not merely
uncontemplated. An earlier draft of this decision reasoned the opposite way —
that null should be accepted as equivalent to omission because "the SDK models
revision as `int | None = None`, so a conformant client that never set it
serializes null." That premise is false: at the pinned adcp 6.6.0,
`UpdateMediaBuyRequest(...).model_dump()` OMITS an unset `revision` entirely
rather than serializing it as `null`, so no conformant client emits `null` in
the first place, and there is no compatibility reason to accept it. `null`
therefore falls through to the same `INVALID_REQUEST` branch as `0` / `"7"` /
`7.5`, consistently across MCP, A2A, and REST.

## Push-notification and reporting webhook delivery

Authoritative sources:

- `docs/building/by-layer/L3/webhooks.mdx` at tag `v3.1.1`
- `dist/schemas/3.1.1/core/push-notification-config.json`
- `dist/schemas/3.1.1/core/mcp-webhook-payload.json`
- `dist/schemas/3.1.1/core/reporting-webhook.json`
- `dist/schemas/3.1.1/media-buy/media-buy-delivery-webhook-result.json`
- `dist/compliance/3.1.1/universal/webhook-emission.yaml`
- `dist/compliance/3.1.1/test-vectors/webhook-signing/`

Those sources define the buyer-facing configuration/payload and signed-webhook
contract. The published webhook-emission storyboard grades emission and the
normative signing contract. When the optional legacy authentication selector is
absent, delivery now uses the SDK's `adcp/webhook-signing/v1` RFC 9421 signer
with `ADCP_WEBHOOK_SIGNING_JWK`; missing or incorrectly scoped key material
fails closed instead of silently emitting an unsigned callback. Explicit
Bearer and legacy HMAC selectors retain their pinned precedence.

The following changes are **local security hardening, ungraded by the AdCP
3.1.1 storyboard**: require HTTPS outside the explicit private-test opt-in,
reject URL userinfo, reject private/reserved DNS results, pin the validated IP
to the socket, refuse environment proxies and redirects, close streamed
responses, reject non-finite JSON, sign and transmit the same exact bytes, and
treat 3xx/4xx/security-policy refusals as permanent rather than retryable.
Registration DNS and delivery DNS/socket work use bounded worker bulkheads with
hard caller deadlines; timed-out work retains its permit until the underlying
blocking call actually finishes, preventing timeout floods from creating an
unbounded executor queue.
These controls must be applied at registration before workflow/database writes
and rechecked at delivery to protect legacy rows and DNS rebinding. They are not
described as AdCP-mandated URL or retry semantics.

Decision for this PR: invalid callback targets fail with a buyer-correctable
validation error before core execution or persistence. Existing legacy HTTP or
otherwise unsafe rows are refused at delivery without retry. The local tests
grade these policies across registration and outbound transport; no dedicated
AdCP compliance step is claimed for them.
