"""Transport-neutral authentication policy for AdCP skills."""

from __future__ import annotations

# The sole list of skills safe to invoke without a resolved principal.
# Every transport imports this exact immutable object so an auth-policy change
# cannot expose or reject a skill on only one boundary.
#
# Grounding: dist/docs/3.1.1/protocol/required-tasks.mdx documents these four
# as plain "Required" discovery tasks, no auth caveat — genuinely public data.
# list_accounts is deliberately excluded: list_accounts.mdx:8 scopes its
# result to "the authenticated agent", and required-tasks.mdx:118 ties it to
# discovering "seller-assigned accounts" for a resolved credential, per
# BR-RULE-055 (docs/test-obligations/business-rules.md).
AUTH_OPTIONAL_SKILLS = frozenset(
    {
        "get_adcp_capabilities",
        "get_products",
        "list_creative_formats",
        "list_authorized_properties",
    }
)
