"""Transport-neutral authentication policy for AdCP skills."""

from __future__ import annotations

# The sole list of skills safe to invoke without a resolved principal.
# Every transport imports this exact immutable object so an auth-policy change
# cannot expose or reject a skill on only one boundary.
AUTH_OPTIONAL_SKILLS = frozenset(
    {
        "get_adcp_capabilities",
        "get_products",
        "list_creative_formats",
        "list_authorized_properties",
    }
)
