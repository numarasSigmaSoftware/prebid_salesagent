"""Guard: MCP tool wrappers must build ToolResult via build_mcp_tool_result().

Disease pattern: ``fastmcp.tools.tool.ToolResult.__init__`` passes
``structured_content`` through ``pydantic_core.to_jsonable_python()``, which
serializes the model's raw core schema directly and does not know about
``AdCPBaseModel``'s ``exclude_none=True`` default. A2A and REST call
``.model_dump()`` explicitly and correctly omit unset optional fields;
constructing ``ToolResult(structured_content=response)`` with the bare model
instead leaks every unset optional as an explicit wire ``null`` on MCP only.

This exact bug class was fixed site-by-site three times (get_products #1266,
get_media_buy_delivery #1575, and 11 more sites in one pass) before this
guard existed — per-site fixing does not stick without something that makes
the next violation impossible to land quietly. `build_mcp_tool_result()` in
``src/core/transport_helpers.py`` is now the one sanctioned way to construct
an MCP ToolResult; this guard rejects any other direct construction under
``src/core/tools/``.

Detection: AST scan for `ToolResult(...)` call expressions under
`src/core/tools/`. `src/core/transport_helpers.py` (where the helper itself
constructs the one legitimate ToolResult) is not scanned.

Self-tests: with an empty allowlist and a clean tree, the guard passing is
indistinguishable from a silently broken matcher, so known-bad/known-good
inline fixtures pin the detector itself.
"""

from __future__ import annotations

import ast

from tests.unit._architecture_helpers import (
    assert_detector_catches_ast_snippets,
    iter_call_expressions,
    iter_module_trees,
    repo_root,
)

# No violations are permitted — this list must stay empty (allowlists only shrink).
_ALLOWED: set[str] = set()

_SCAN_DIRS = [repo_root() / "src" / "core" / "tools"]


def _find_violations_in_tree(tree: ast.Module) -> list[int]:
    """Line numbers of direct `ToolResult(...)` construction."""
    return [call.lineno for call in iter_call_expressions(tree, name="ToolResult")]


def _scan_violations() -> list[str]:
    violations: list[str] = []
    for tree, rel_path in iter_module_trees(_SCAN_DIRS):
        for lineno in _find_violations_in_tree(tree):
            entry = f"{rel_path}:{lineno}"
            if entry not in _ALLOWED:
                violations.append(entry)
    return violations


_KNOWN_BAD_SNIPPETS = {
    "bare-construction": ("ToolResult(content=str(response), structured_content=response)"),
    "bare-construction-with-model-dump": (
        'ToolResult(content=str(response), structured_content=response.model_dump(mode="json"))'
    ),
}

_KNOWN_GOOD_SNIPPETS = {
    "via-helper": "build_mcp_tool_result(str(response), response)",
}


class TestNoDirectToolResultConstruction:
    """Structural guard: MCP wrappers return through build_mcp_tool_result()."""

    def test_no_direct_toolresult_construction_under_tools(self):
        """Every ToolResult in src/core/tools/ must go through build_mcp_tool_result().

        A direct ``ToolResult(...)`` call — with or without ``.model_dump()`` —
        is exactly the shape that silently regresses back to the wire-null-leak
        bug; the helper is the only place that decision should be made.
        """
        violations = _scan_violations()
        assert not violations, (
            "Direct ToolResult(...) construction under src/core/tools/ — use "
            "build_mcp_tool_result(content, response) from src.core.transport_helpers "
            "instead:\n  " + "\n  ".join(violations)
        )

    def test_detector_flags_known_bad_fixtures(self):
        """Positive self-test: every known-bad inline form MUST be flagged.

        Pins the matcher against silent breakage — with an empty allowlist and
        a clean tree, the guard passing proves nothing about the detector.
        """
        assert_detector_catches_ast_snippets(_find_violations_in_tree, snippets=_KNOWN_BAD_SNIPPETS)

    def test_detector_passes_known_good_fixtures(self):
        """Negative self-test: the sanctioned helper call must NOT be flagged."""
        flagged = {
            label: _find_violations_in_tree(ast.parse(source, filename=f"<known-good:{label}>"))
            for label, source in _KNOWN_GOOD_SNIPPETS.items()
        }
        false_positives = {label: lines for label, lines in flagged.items() if lines}
        assert not false_positives, f"Detector flagged known-good snippet(s): {false_positives}"
