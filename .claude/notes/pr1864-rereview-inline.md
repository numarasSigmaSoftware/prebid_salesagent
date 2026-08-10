## src/core/helpers/account_helpers.py:216
**BLOCKER — restoring the original defect here reddens nothing in three suites.**

Every new behavioural test grades the buy-keyed derivation. The identity-keyed derivation — the defect this PR opens with — has no oracle. Three independent mutations, each two-sided against a verified non-empty baseline:

| mutation | baseline | mutated |
|---|---|---|
| `ResolvedAccount(account.account_id, False)` at `:216`/`:270` | unit 5569 / integration 2225 / bdd 1747 | identical, 0 failed, 0 mypy errors |
| `media_buy_create.py:2743` + `:3625` -> `sandbox=False` | 7 sandbox test files: 192 | 192; full `tests/unit/`: 5569 |
| `:3625` + `operations.py:254` -> `sandbox=False` | unit+harness 5706 | 5706 |

`git grep "identity.sandbox" tests/` returns only `= False` assignments, comments, and `is False` assertions — no test anywhere drives `identity.sandbox=True`. `test_resolve_account.py:44,107` assert `result.sandbox is False` twice and never `True`.

Two written claims are false at head: the PR body's "Every forwarding site (**media-buy creation**, ...) is individually mutation-tested", and `test_sandbox_production_paths.py:186-192`'s "One path remains graded only by its derivation: admin media-buy detail."

The infrastructure is already here — an MCP call with `account={"account_id": "acc_sbx"}` against `AccountFactory(sandbox=True)`, using this PR's own `_seed_two_accounts` fixture, yields `identity.sandbox=True`. Cheapest complete form: `assert result.sandbox is True` on a `create_media_buy` wire test, plus a `sandbox=True` case in `test_resolve_account.py`.

## tests/unit/test_architecture_get_adapter_sandbox.py:97
**SHOULD-FIX — this arm is inert; it does not catch the defect it was written for.**

`isinstance(kw.value, ast.Attribute) and kw.value.attr == "sandbox"` — all four scanned modules pass a bare `ast.Name` (`sandbox`, `is_sandbox`, `partition_is_sandbox`, `_mb_sandbox`), so operand 1 is False everywhere and `offenders` is empty as shipped.

Measured: deleting the entire detection body leaves `3 passed`. Reverting `performance.py:71` to `sandbox = identity.sandbox` — the original defect in that module's own idiom — leaves the guard green; so do the `media_buy_delivery.py` equivalent and `sandbox=bool(identity.sandbox)`. Only an inline literal reddens.

Fix: resolve `sandbox=<Name>` back to its assignment within the function and flag when the RHS reaches `identity.sandbox`; add a positive/negative self-test — a six-line paste of the pattern already at `:66`. While there, `base == "identity"` also misses `self.identity.sandbox`.

## tests/unit/test_architecture_get_adapter_sandbox.py:41
**SHOULD-FIX — this arm grades presence, not value.**

Replacing each site's expression with `sandbox=False` — a hard-wired live dispatch, the cheapest way to satisfy the predicate — leaves the guard green at **12 of 12** sites. The PR's own new sandbox unit tests stay green at 8 of 12. For `media_buy_delivery.py:217 -> sandbox=False` the entire unit+harness gate is byte-identical to control (5706 passed).

The integration tier does catch these (delivery -> 2 failed; `media_buy_create.py:573` -> 1; `:1191`/`:1248` -> 1 each), which is why this is not rated higher.

Fix: reject a bare `ast.Constant` as the `sandbox=` value unless the site is in `KNOWN_EXEMPT` with a written reason — the exemption dict and the AST node are both already in hand, roughly four lines.

## tests/unit/test_architecture_get_adapter_sandbox.py:80
**SHOULD-FIX — the scan set omits 3 of the 5 paths the failure message names.**

The arm-1 text names "update, performance, creative push, the approval executor, admin routes"; `_BUY_KEYED_MODULES` is update / performance / list / delivery.

Measured: injecting `sandbox=identity.sandbox` at `media_buy_create.py:1248` (approval executor) leaves the guard at `3 passed`; same at `src/admin/blueprints/operations.py:254`. Both carry the literal defect this arm exists to catch. Also uncovered: `media_buy_create.py:1186`, `:1349`.

Fix: derive the set rather than hardcoding it — e.g. any module whose `get_adapter` call is not reached from a wrapper calling `enrich_identity_with_account` — and add a companion asserting the set covers every module the arm-1 message names.

## src/core/tools/products.py:711
**SHOULD-FIX — `identity.sandbox` is structurally always False here** (1 of 3 sites; see also `capabilities.py:120` and `creative_formats.py:512`).

`transport_helpers.py:152` is the sole writer in `src/`; its 7 call sites are `creatives/sync_wrappers.py:57,:118`, `media_buy_create.py:4537,:4622`, `media_buy_delivery.py:829,:885`, `routes/api_v1.py:385`. `products.py` is not among them and contains zero occurrences of `.account`.

Buyer-reachable: `GetProductsWholesaleRequest.account` exists in SDK 6.6.0 and in the 3.1.1 request schema — accepted and never read. So a sandbox `get_products` selects the live tenant adapter, and `get_adapter` then runs `AdapterConfigRepository.find_by_tenant()` + `build_gam_config_from_adapter`, loading real credentials.

No outbound call — `dry_run=True` skips `GAMClientManager` and `get_supported_pricing_models()` is local — so the spec MUST at `sandbox.mdx:229` is not breached. The comment at `adapter_helpers.py:132` ("not its API, not even its credentials") is. Tracked in #1874 §3, whose acceptance criterion already covers the honest outcome.

## src/core/tools/capabilities.py:120
**SHOULD-FIX — same dead-source shape as `products.py:711`, and undisclosed.**

`capabilities.py` never calls `enrich_identity_with_account`, so `identity.sandbox` is always False here too. `GetAdcpCapabilitiesRequest` has no `account` field at all — so unlike `get_products`, the kwarg is dead by protocol rather than by omission.

#1874 names `get_adcp_capabilities` only to exclude it from the response-marker work ("no `sandbox` field in the SDK at all"). That is correct about the marker and says nothing about the routing, which is a different defect: a real adapter is constructed from real `AdapterConfig` for a sandbox account.

Fix: replace the dead expression with an explicit `False` plus a comment naming the absent request field, and add this site to #1874's scope note so the gap is not mistaken for coverage.

## src/core/tools/creative_formats.py:512
**SHOULD-FIX — this replaced a reachable source with a dead one, and two written claims say otherwise.**

Before: `if identity and identity.testing_context and identity.testing_context.dry_run: sandbox_flag = True`. After: `True if (identity and identity.sandbox) else None`. This module never enriches, and `ListCreativeFormatsRequest` has no `account` field in SDK 6.6.0, so `sandbox_flag` is now **always `None`**. All three entry points (`:594` MCP, `:617` A2A, `routes/api_v1.py:257` REST) pass an unenriched identity.

Ungraded as well: collapsing the whole expression to `None` reddens nothing (535 passed and 149 passed in two separate selections). The only live scenario grades the `None` branch, so it survives the mutation.

Two claims are false at head: the PR body's "`create_media_buy` and `list_creative_formats` already do this correctly", and #1874's opening "(`list_creative_formats` already had one before that PR)" — which is the stated reason this path is out of that issue's scope. Correct marker adoption is 1 live of 5 eligible.

Removing the header source is spec-correct (`sandbox.mdx:221`). Upstream cause of the dead replacement: the creative-namespace `list-creative-formats-request.json` declares `account`, but the media-buy copy and the SDK do not — worth saying in the body, since it is not a repo bug.

## src/core/tools/media_buy_create.py:801
**SHOULD-FIX — the decline on this line rests on a premise the code falsifies.**

The reply says adopting the seam "opens a second session inside an already-open transaction." A UoW is already bound and live at this line: `:768` `with MediaBuyUoW(tenant_id) as uow:`, `:771` `session = uow.session`, `:790` `media_buy = session.scalars(stmt).first()` (attached), and `:801` sits inside that block at 12-space indent. `uow.accounts` is built on that same session at `uow.py:188`, and the `with` block predates this PR (`origin/main` has it at `:757`).

This is your own stated criterion — "the critique lands where a UoW was in scope and went unused" — and it is the highest-consequence holdout, since the approval executor dispatches an approved buy to the real ad server.

Fix: `mb_sandbox = uow.sandbox_mode(media_buy)`. The `DetachedInstanceError` concern is unaffected — it reads `media_buy.account_id` synchronously inside the live session and returns a plain `bool` — and it skips the second lookup `sandbox_mode_by_id` would incur.

## src/admin/blueprints/operations.py:254
**SHOULD-FIX — the decline holds, but not for the reason given, and a ~5-line alternative exists.**

The conclusion is right; the reasoning ("same reasoning as the executor site") is not — the executor site does have a UoW in scope (see `media_buy_create.py:801`).

The real mechanism, probe-verified: the app uses `scoped_session` with `expire_on_commit=True` (`database_session.py:150-151,225-226,242-243`), so calling `_get_media_buy_delivery_impl` inside this route's `with get_db_session()` resolves to the **same** Session, and its `MediaBuyUoW.__exit__` commits and closes it under the route. Every `render_template` argument at `:288-301` then raises `DetachedInstanceError`, the outer `except Exception` at `:302` catches, and the page 500s. Second cost: the template consumes the adapter shape (`totals`/`by_package`/`currency`), not the tool's `aggregated_totals`/`media_buy_deliveries`.

Fix that closes the gap without touching session lifetime: add `sandbox_mode_for_buy(accounts, media_buy) -> bool` to `account_helpers`, have `BuyKeyedSandboxMixin.sandbox_mode` delegate to it, and call it from here and from `media_buy_create.py:801`. The mixin's existing lazy import already documents why it must live in `account_helpers`, so no new cycle.

## src/admin/blueprints/operations.py:255
**SHOULD-FIX — this `sandbox=` value has no oracle, and the block never executes in any suite.**

Mutating it to `sandbox=False` keeps the guard green (the predicate is value-blind). The block is gated by `:219` `status in ["active","approved","completed"]`, and the only tests that load this route (`tests/admin/test_sell_readiness_browser.py:381,399`) use `_create_pending_media_buy`, which asserts `pending_approval` at `:253`. There is no `tests/admin/test_operations_blueprint.py`.

The PR concedes this at `test_sandbox_production_paths.py:193`; recording it as its own item so it does not ride along as a mitigation of the fork above.

Fix: one `tests/admin/` test — an `active` buy on a sandbox account asserts the route builds `MockAdServerAdapter`, plus the live negative control. The status gate is the only obstacle and it is fixture-settable.

## src/core/tools/media_buy_delivery.py:210
**SHOULD-FIX — half of this thread landed; the named consequence survives.**

`:208` is now `dict[bool, AdServerAdapter]`, but `:210` is still `-> Any`. Every call goes through this accessor (sole consumer at `:366`), so annotating the dict alone changes nothing at the call site — which is exactly what the original comment named: "`_adapter_for(...).get_media_buy_delivery(...)` is unchecked." mypy is green either way, which is why it reads as fixed.

Swept: `git diff origin/main..HEAD -- src | grep '^+.*-> Any'` returns this line only; `media_buy_list.py` uses a direct loop with no accessor.

Fix: `-> AdServerAdapter` (already imported at `:86`).

## src/core/database/repositories/uow.py:127
**SHOULD-FIX — this docstring and the guard message overstate adoption and misdirect.**

Adoption is **3 of the 5** sites this docstring enumerates. Adopted: `media_buy_update.py:416`, `performance.py:71`, `media_buy_create.py:1353`. Holdouts: `media_buy_create.py:801`, `operations.py:254`. `:127` names "the admin detail route" as a site this serves; `:131` claims the derivation "is decided in one place rather than re-derived at each call site".

The guard's failure text (`test_architecture_get_adapter_sandbox.py:60`) compounds it: "(... creative push, the approval executor, admin routes) use `MediaBuyUoW.sandbox_mode_by_id(id)`". Creative push uses `AdminCreativeUoW` — your own note says a `MediaBuyUoW`-only placement would have been an `AttributeError` in production — and following that instruction on the admin route 500s the page.

No behavioural drift exists today: both holdouts call the same `account_is_sandbox` this mixin calls, so the risk is prospective. The live defect is a message that sends the next contributor to a remedy that breaks. Name `BuyKeyedSandboxMixin`, and either land the shared function so all 5 adopt or reconcile `:127-132` with the real count.

## src/core/helpers/account_helpers.py:121
**SHOULD-FIX — this code may mischaracterize the condition.** Two readings of the pinned spec, both recorded; your call.

**Recovery class is correct.** 3.1.1 `core/error.json` names "account not found" as the `terminal` exemplar, and the condition needs operator repair.

**The code is arguable.** The condition table at `dist/docs/3.1.1/building/by-layer/L2/accounts-and-agents.mdx:464` reads `| ACCOUNT_NOT_FOUND | account_id doesn't exist or agent lacks access | Check account reference, re-run sync_accounts |` — a buyer-directed remedy. On this path the buyer sent no account reference; the failure is a seller-side dangling `media_buys.account_id`, and neither half of that remedy applies. `CONFIGURATION_ERROR`'s description is the declared seller-side class ("the buyer cannot fix it, retrying will not help, and reporting to the seller's operator is the only remediation").

Recovery is `terminal` under both candidate codes, so a buyer's autonomous recovery is unaffected either way — only the code, message and suggestion misdirect. Reachability is also very low (the composite FK cannot orphan a row, and `sync_accounts --delete_missing` only closes accounts).

Second-order: as a typed error, `record_boundary_error` (`tool_error_logging.py:212-220`) logs WARNING with no `exc_info`, so a data-integrity corruption gets buyer-error-grade telemetry with no traceback.

If you switch to `CONFIGURATION_ERROR`, check its wire-placement paragraph (HTTP 5xx) against the 404 default here. If you keep `ACCOUNT_NOT_FOUND`, carry the spec's canonical suggestion and a comment naming the deliberate deviation.

## src/core/tools/creative_formats.py:509
**SHOULD-FIX — this comment quotes a MUST NOT the code honours at 1 of 16 sites.**

`testing_hooks.py:122` reads `x-dry-run` into `testing_ctx.dry_run`, which drives `get_adapter(dry_run=...)` at `media_buy_create.py:572`/`:2743`, `media_buy_delivery.py:214`, `media_buy_list.py:201`, `media_buy_update.py:481`; skips setup validation at `media_buy_create.py:2112`; skips manual approval at `:2768`; forces the simulated lifecycle at `:3574`; and gates `apply_testing_hooks` at `testing_hooks.py:660`. `sandbox.mdx:218-223` deprecates all three headers, and `testing_hooks.py:121`/`:131` read the other two.

Pre-existing, and the PR discloses the rest under Known gaps — but a reader takes this comment as repo-wide policy. The quotation also omits the half that names the remedy: "Sellers SHOULD ignore the headers entirely and MAY log a deprecation warning."

Fix: narrow the comment to what this line does, and file the header sweep with a scope list — body prose is not an owner.

## src/core/helpers/adapter_helpers.py:131
**SHOULD-FIX — this branch builds a different mock adapter than the live mock path at `:152-159`.**

Constructed both on the tree: sandbox gives `manual_approval_required = False`, live mock gives `True`. Base defaults `False` (`adapters/base.py:229`); the live path defaults NULL -> `True` "for safety". `dry_run` diverges too — sandbox passes the caller's `dry_run`, live passes `config_row.mock_dry_run or False`.

`_create_media_buy_impl:2749-2751` reads `adapter.manual_approval_required`. The tenant flag dominates, so this bites only when `human_review_required=False` and the adapter config would have required approval — a sandbox buy then auto-executes where a live one queues.

Low security impact, since the sandbox side has no real side effects. But it is exactly the fork a test asserting "the mock was selected" cannot see. Fix: build the sandbox config from the same dict the `mock` branch uses, or extract `_mock_adapter_config(dry_run, config_row=None)`.

## pyproject.toml:27
**SHOULD-FIX — this bump is from an open sibling PR, not main, and it is the superseded version.**

`git merge-base --is-ancestor dd0bee904 origin/main` -> NO. `origin/main:pyproject.toml` still reads `aiohttp>=3.14.0` / `cryptography>=46.0.7`. The merge commit `07c6d3894` says "pull in aiohttp/cryptography CVE bump **from main**".

`dd0bee904` **is** an ancestor of open PR #1865, whose head `a20f41d83` adds "fix: disambiguate the aiohttp advisory comment delimiter" — correcting `" + "` to `";"` on this exact line, because the file reserves `" + "` for same-scheme siblings and `";"` for a distinct advisory group. This branch does not carry that commit, so merging it lands the form #1865's review already rejected, and whichever merges second conflicts.

The stated rationale (unblocking Security Audit / pip-audit) is worth re-testing — both pass on this head.

Fix: drop `07c6d3894`/`dd0bee904` and let #1865 land on its own, or carry `a20f41d83` too and correct the merge commit's provenance.

## src/admin/blueprints/operations.py:285
**SHOULD-FIX — two scanning threads on this line are unanswered; one is real-but-bounded, one is not this file.**

**Alert 874** is real and PR-attributed: one instance, on `refs/pull/1864/merge`, created 2026-08-05T03:57, not present on main. But the sink pre-exists — `origin/main` has it at `:277`, and blame attributes `:285` to the commit that added only `exc_info=True`. Bounded: the route is `@require_tenant_access()` and `media_buy_id` must have matched `repo.get_by_id` at `:118` (404 otherwise), so the value is a server-generated `mb_<uuid4[:12]>`.

One thread left open, worth checking before dismissing: the Kevel and Broadstreet adapters derive `media_buy_id` from `request.po_number` (`kevel.py:240`, `broadstreet/adapter.py:364`), which is buyer-supplied. Whether that value ever reaches the `media_buys` PK column was not traced, and it is what would turn 874 into a true positive.

`log_safe(media_buy_id)` is one call, matches the repo convention at `logging_config.py:236`, and clears it either way.

**Alert 481** is mis-anchored: it is `src/services/dynamic_pricing_service.py:56`, created 2026-06-08, with 8 instances all in that file across main and 7 other PRs — none on this file, none on this PR. Worth saying so in the thread so it does not read as an unanswered security item.

## src/core/database/repositories/uow.py:155
**NIT — this fails open on a missing buy at one of its two callers.**

`sandbox_mode(None)` returns `False` (LIVE), while `account_is_sandbox` **raises** for a non-null unresolvable account one function below — whose docstring calls returning `False` there "fail-OPEN ... the exact failure this module exists to prevent". Two not-found policies one call apart.

`performance.py:68` discharges the precondition (`_verify_principal` -> `get_by_id_or_raise`). `media_buy_create.py:1353` does not: `get_adapter(..., sandbox=False)` is constructed at `:1349`, before the buy-existence check at `:1359`.

Bounded — the adapter is never invoked, because `get_package` joins `MediaBuy` and returns None for the same missing row, so the function returns at `:1361`. The safety is supplied by caller ordering rather than by the seam created to own the decision.

Fix: use `get_by_id_or_raise` here, keeping the `None`-buy -> live legacy allowance only on the explicit `sandbox_mode(buy)` overload where the caller has already decided.

## tests/integration/test_sandbox_delivery_account_scoping.py:156
**NIT — this docstring's mechanism does not reproduce.**

"deleting an account nulls every referencing buy" is false. Probe against real Postgres, replicating the composite FK at `models.py:968-972`: `DELETE FAILED: IntegrityError (psycopg2.errors.NotNullViolation) null value in column "tenant_id" ... violates not-null constraint`. A multi-column FK with `ON DELETE SET NULL` and no column list nulls **all** referencing columns, and `MediaBuy.tenant_id` is NOT NULL — so the delete itself is rejected.

The conclusion ("unreachable through any normal write") is actually stronger than stated, but the stated mechanism is wrong and will be quoted forward.

Fix: state the observed behaviour — the composite FK makes the account delete fail outright, and `sync_accounts --delete_missing` only sets `status="closed"` (`accounts.py:655`) while `get_by_id` applies no status filter, so no supported write can orphan the reference.

## tests/unit/test_sandbox_account_isolation.py:97
**NIT — this grades two Python built-ins, not the coercion it names.**

`assert ResolvedAccount("acc_1", bool(None)).sandbox is False` computes `bool()` in the test body and reads a NamedTuple field. Production's `bool(account.sandbox)` at `account_helpers.py:216`/`:270` is never invoked. Dropping `bool()` at both production sites leaves this green.

Mitigation, found by the same mutation: that drop emits 2 mypy `arg-type` errors, and `make quality` runs mypy — so the coercion **is** mechanically enforced. The exposure is a dead test, not an unguarded invariant. A genuinely-graded namesake was added at `test_sandbox_propagation_flows.py:92`, which is likely why this read as addressed.

The enclosing class `TestResolvedAccountCarriesMode` and its docstring ("The flag was previously discarded at the one place it was known") promise the resolution seam this test never touches.

Fix: rewrite it to drive `resolve_account` against a NULL-`sandbox` account — which also closes part of the BLOCKER's negative side — rather than delete it.

## src/core/transport_helpers.py:152
**NIT — this is the sole writer, which makes the description's "single transport funnel" claim false.**

The PR body says `sandbox` is "set at the single transport funnel every protocol passes through". This line is indeed the only place `ResolvedIdentity.sandbox` is ever written in `src/` — but its caller `enrich_identity_with_account` is invoked from **7** per-tool/per-wrapper sites: `creatives/sync_wrappers.py:57,:118`, `media_buy_create.py:4537,:4622`, `media_buy_delivery.py:829,:885`, `routes/api_v1.py:385`.

That was raised in round 1, and neither contested nor corrected. There is no mechanism making it true: a new tool that carries an account reference and forgets to call the enrichment gets `sandbox=False` for a real sandbox account, and the new guard still passes. `products.py:711` and `capabilities.py:120` are the live instances today.

Fix: either replace the sentence with the real enrichment-site count, or add the mechanism that makes it true.

## src/core/tools/media_buy_create.py:1353
**NIT — the adapter is constructed before the buy-existence check.**

`uow.sandbox_mode_by_id(media_buy_id)` returns `False` (live) for an id that resolves to no row, and `get_adapter(..., sandbox=False)` is built at `:1349` — while the buy-existence check (`get_package`/`platform_order_id`) is at `:1359-1365`.

Bounded, so this is a NIT rather than a fail-open in practice: `get_package` joins `MediaBuy` with the same tenant filter, returns None for the same missing row, and the function returns at `:1361` before the adapter is invoked.

Recording it because the ordering, not the seam, is what supplies the safety here — see the `uow.py:155` thread for the seam-level fix.

