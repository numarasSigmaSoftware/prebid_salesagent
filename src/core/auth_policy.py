"""Transport-neutral authentication policy for AdCP skills."""

from __future__ import annotations

# The sole list of skills safe to invoke without a resolved principal.
# Every transport imports this exact immutable object so an auth-policy change
# cannot expose or reject a skill on only one boundary.
#
# Membership is unchanged from the per-transport lists this replaced —
# consolidation, not re-policy.
#
# Grounded against the pinned 3.1.1 spec PER ENTRY, because the four do not in
# fact share one justification:
#
#   get_adcp_capabilities / get_products / list_creative_formats
#     building/by-layer/L2/authentication.mdx "Public Operations (No
#     Authentication Required)" names exactly these three — the only
#     positively-stated public set in the spec. get_products appears in BOTH
#     lists: public with a partial catalog, authenticated for full access.
#
#   list_authorized_properties
#     NOT spec-grounded, and deliberately kept anyway. The spec REMOVED this v2
#     task in v3: its sole mention across the 3.1.1 docs is the migration note
#     in protocol/get_adcp_capabilities.mdx ("removed in v3"; its fields moved
#     under media_buy.portfolio). It is here because both transports already
#     treated it as public before this list existed, and revoking a surface
#     buyers may still call belongs with the v2-compat sunset, not with an
#     error-boundary change.
#
# list_accounts is EXCLUDED on positive spec text, not on absence from a list:
# accounts/tasks/list_accounts.mdx opens "Returns all accounts the AUTHENTICATED
# agent can operate on this vendor agent", and protocol/required-tasks.mdx marks
# it Conditional for account-id namespaces with "require_operator_auth: true".
# Per BR-RULE-055 (docs/test-obligations/business-rules.md).
#
# Absence from "Public Operations" is NOT itself an argument for exclusion: that
# list omits most operations — including list_authorized_properties above — so
# reading it as a closed prohibition would contradict this very set.
AUTH_OPTIONAL_SKILLS = frozenset(
    {
        "get_adcp_capabilities",
        "get_products",
        "list_creative_formats",
        "list_authorized_properties",
    }
)
