Provenance correction on four roll-up items — none is a regression from this PR, and I should have labeled them. All four remain tracked against #1874/#1875 rather than being work for this branch.

- `@T-UC-004-identify-account-scope` and `@T-UC-004-sandbox-natural-key`: `BR-UC-004-deliver-media-buy-metrics.feature` is untouched by this diff, and both scenarios are dormant on `origin/main` already. The point stands as a coverage observation — they are the designated graders for `media_buy_delivery.py:260`, which is new — but the dormancy is not yours.
- `then_no_sandbox_field` and `then_no_real_api_calls` / `then_no_billing`: `then_success.py` is untouched, and both step bodies are byte-identical on `origin/main`. The forward-looking risk is real (they go green vacuously once the echo lands) but the steps predate this branch.
- The two `given_sandbox_account` implementations: `given_auth.py` and `uc019_query_media_buys.py` are both untouched, and both are byte-identical on `origin/main`. Entirely pre-existing; #1875 is the right home, as you said.
- The call matcher in `tests/unit/_architecture_helpers.py:170`: also untouched by this diff, and 30 guard modules import `iter_call_expressions`. Widening it is a cross-guard change with its own blast radius, not a tweak to the guard this PR adds. Treat it as context for how much the new guard can be expected to catch, not as a change request here.

Unchanged: the xfail-reason staleness is fairly attributed — the entries predate the branch, but this PR is what made three of the reason strings false. Likewise the `media_buy_list.py:241` fallback and the 8-patch scaffold were already labeled pre-existing in the original comment.

