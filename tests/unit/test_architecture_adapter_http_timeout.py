"""Guard: every adapter ``requests`` HTTP call must pass a bounded ``timeout``.

Bounded-execution contract: adapter calls must release worker resources in a
bounded time. An unbounded ad-server call can otherwise leave an update lease
and its worker occupied until the TCP stack gives up. A ``requests`` call without
a connect+read ``timeout`` defaults to blocking forever. This AST guard fails the
build on any ``requests.<method>(...)`` in ``src/adapters`` that omits ``timeout=``
— use :data:`src.adapters.constants.ADAPTER_HTTP_TIMEOUT`.

``requests.Session(...)`` construction is flagged too: calls made through a
session-bound variable (``session.post(...)``) are invisible to the per-call
matcher (it anchors on the ``requests`` module name), so a Session would be a
silent escape hatch from the timeout contract. No adapter uses one today; if one
ever must, it needs its own per-call timeout discipline plus an explicit
allowlist entry here.

Non-``requests`` clients (googleads SOAP, broadstreet client) bound themselves at
construction and are out of scope for this text-level guard.
"""

import ast

from tests.unit._architecture_helpers import (
    assert_violations_match_allowlist,
    iter_call_expressions,
    repo_root,
    safe_parse,
)

_REQUESTS_METHODS = {"post", "get", "put", "delete", "patch", "request", "head", "options"}

# Pre-existing unbounded calls: (relative_file_path, line). Empty — every adapter
# HTTP call is bounded. It may only shrink; a new entry means a new unbounded call.
ALLOWLIST: set[tuple[str, int]] = set()


def _requests_module_names(tree: ast.AST) -> set[str]:
    """Names the ``requests`` module is bound to in this file.

    Always includes ``"requests"``; also resolves aliased imports
    (``import requests as r`` -> ``r``) so a call through the alias is not a
    silent escape from the timeout matcher.
    """
    names = {"requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "requests" and alias.asname:
                    names.add(alias.asname)
    return names


def _requests_function_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Directly imported request methods and Session constructor names."""
    methods: set[str] = set()
    sessions: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in {"requests", "requests.api", "requests.sessions"}:
            continue
        for alias in node.names:
            bound = alias.asname or alias.name
            if node.module in {"requests", "requests.api"} and alias.name in _REQUESTS_METHODS:
                methods.add(bound)
            if alias.name in {"Session", "session"}:
                sessions.add(bound)
    return methods, sessions


def _requests_sessions_module_names(tree: ast.AST) -> set[str]:
    """Names bound to the ``requests.sessions`` module itself."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "requests.sessions" and alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "requests":
            for alias in node.names:
                if alias.name == "sessions":
                    names.add(alias.asname or alias.name)
    return names


def _requests_method_aliases(tree: ast.AST, module_names: set[str]) -> set[str]:
    """Local names assigned from a recognized ``requests`` method."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if value is None:
            continue
        chain = _attribute_chain(value)
        if not chain or chain[0] not in module_names:
            continue
        if len(chain) not in {2, 3} or chain[-1] not in _REQUESTS_METHODS or chain[1:-1] not in ([], ["api"]):
            continue
        aliases.update(target.id for target in targets if isinstance(target, ast.Name))
    return aliases


def _requests_session_aliases(tree: ast.AST, module_names: set[str], sessions_module_names: set[str]) -> set[str]:
    """Local names assigned from a recognized ``requests.Session`` constructor."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if value is None:
            continue
        chain = _attribute_chain(value)
        if not chain:
            continue
        direct_session = (
            chain[0] in module_names and chain[-1] in {"Session", "session"} and chain[1:-1] in ([], ["sessions"])
        )
        imported_sessions_module = (
            len(chain) == 2 and chain[0] in sessions_module_names and chain[1] in {"Session", "session"}
        )
        if direct_session or imported_sessions_module:
            aliases.update(target.id for target in targets if isinstance(target, ast.Name))
    return aliases


def _attribute_chain(node: ast.expr) -> list[str] | None:
    """Flatten ``requests.sessions.Session``-style attribute chains."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return list(reversed(parts))


def _is_none_literal(value: ast.expr) -> bool:
    """True for a literal ``None`` (``timeout=None`` is as unbounded as no timeout)."""
    return isinstance(value, ast.Constant) and value.value is None


def _unbounded_requests_calls(tree: ast.AST) -> list[int]:
    """Line numbers of unbounded ``requests`` usage.

    Flags ``requests.<method>(...)`` calls that omit a ``timeout`` kwarg OR pass
    ``timeout=None`` (which blocks forever exactly like omitting it), and any
    ``requests.Session(...)`` construction (calls through a session variable
    evade the per-call matcher, so a Session is an escape hatch — see module
    docstring). Recognizes aliased ``requests`` imports.
    """
    module_names = _requests_module_names(tree)
    direct_methods, direct_sessions = _requests_function_names(tree)
    sessions_module_names = _requests_sessions_module_names(tree)
    method_aliases = _requests_method_aliases(tree, module_names)
    session_aliases = _requests_session_aliases(tree, module_names, sessions_module_names)
    hits: list[int] = []
    for node in iter_call_expressions(tree):
        if isinstance(node.func, ast.Name):
            if node.func.id in direct_sessions | session_aliases:
                hits.append(node.lineno)
            elif node.func.id in direct_methods | method_aliases:
                timeout_kw = next((kw for kw in node.keywords if kw.arg == "timeout"), None)
                if timeout_kw is None or _is_none_literal(timeout_kw.value):
                    hits.append(node.lineno)
            continue

        chain = _attribute_chain(node.func)
        if not chain:
            continue
        if len(chain) == 2 and chain[0] in sessions_module_names and chain[1] in {"Session", "session"}:
            hits.append(node.lineno)
            continue
        if chain[0] not in module_names:
            continue
        if chain[-1] in {"Session", "session"} and chain[1:-1] in ([], ["sessions"]):
            hits.append(node.lineno)
        elif len(chain) in {2, 3} and chain[-1] in _REQUESTS_METHODS and chain[1:-1] in ([], ["api"]):
            timeout_kw = next((kw for kw in node.keywords if kw.arg == "timeout"), None)
            if timeout_kw is None or _is_none_literal(timeout_kw.value):
                hits.append(node.lineno)
    return hits


def test_adapter_requests_calls_are_bounded():
    repo = repo_root()
    found: set[tuple[str, int]] = set()
    for py_file in (repo / "src" / "adapters").rglob("*.py"):
        tree = safe_parse(py_file)
        if tree is None:
            continue
        rel = str(py_file.relative_to(repo))
        for line in _unbounded_requests_calls(tree):
            found.add((rel, line))

    assert_violations_match_allowlist(
        found,
        ALLOWLIST,
        fix_hint=(
            "Pass timeout=ADAPTER_HTTP_TIMEOUT (from src.adapters.constants) to every "
            "requests call so a hung ad server cannot pin an update_media_buy row lock."
        ),
    )


# ── Guard self-tests: synthetic snippets through the matcher ─────────────────
# Mirrors test_architecture_media_buy_status_writes.py — a guard whose matcher
# silently stops matching is a green build with no protection.


def _snippet_hits(snippet: str) -> list[int]:
    """Run the matcher over a synthetic source snippet."""
    return _unbounded_requests_calls(ast.parse(snippet))


def test_guard_detects_unbounded_requests_call():
    """Known-bad: a ``requests`` call without ``timeout=`` is flagged at its line."""
    assert _snippet_hits('import requests\n\n\ndef f(url):\n    return requests.post(url, json={"a": 1})\n') == [5]


def test_guard_detects_session_construction():
    """Known-bad: ``requests.Session()`` is flagged — the per-call matcher cannot
    see calls through the session variable, so construction itself is the seam."""
    assert _snippet_hits("import requests\n\n\ndef f():\n    s = requests.Session()\n    return s\n") == [5]


def test_guard_detects_timeout_none():
    """Known-bad: ``timeout=None`` blocks forever, so it is flagged like an omission."""
    assert _snippet_hits("import requests\n\n\ndef f(url):\n    return requests.post(url, timeout=None)\n") == [5]


def test_guard_detects_aliased_requests_call():
    """Known-bad: an aliased ``import requests as r`` call without a timeout is flagged —
    the module-name anchor must follow the alias, not just the literal ``requests``."""
    assert _snippet_hits("import requests as r\n\n\ndef f(url):\n    return r.get(url)\n") == [5]


def test_guard_detects_locally_aliased_requests_method():
    """Known-bad: assigning ``requests.post`` to a local cannot hide an unbounded call."""
    assert _snippet_hits("import requests\n\n\ndef f(url):\n    send = requests.post\n    return send(url)\n") == [6]


def test_guard_detects_locally_aliased_session_constructor():
    """Known-bad: local aliases of either Session constructor form are escape hatches."""
    assert _snippet_hits(
        "import requests\n\n\ndef f():\n    make_session = requests.Session\n    return make_session()\n"
    ) == [6]
    assert _snippet_hits(
        "from requests import sessions\n\n\ndef f():\n    make_session = sessions.Session\n    return make_session()\n"
    ) == [6]


def test_guard_detects_lowercase_session_factory():
    """Known-bad: Requests' lowercase session factory returns the same escape hatch."""
    assert _snippet_hits("import requests\n\n\ndef f():\n    return requests.session()\n") == [5]
    assert _snippet_hits("from requests import session\n\n\ndef f():\n    return session()\n") == [5]


def test_guard_detects_directly_imported_request_call():
    """Known-bad: ``from requests import get`` cannot evade the timeout guard."""
    assert _snippet_hits("from requests import get\n\n\ndef f(url):\n    return get(url)\n") == [5]


def test_guard_detects_requests_api_call():
    """Known-bad: ``requests.api.get`` cannot bypass the module matcher."""
    assert _snippet_hits("import requests\n\n\ndef f(url):\n    return requests.api.get(url)\n") == [5]


def test_guard_detects_directly_imported_requests_api_call():
    """Known-bad: ``from requests.api import get`` cannot bypass the guard."""
    assert _snippet_hits("from requests.api import get\n\n\ndef f(url):\n    return get(url)\n") == [5]


def test_guard_detects_nested_session_constructor():
    """Known-bad: ``requests.sessions.Session`` is the same escape hatch."""
    assert _snippet_hits("import requests\n\n\ndef f():\n    return requests.sessions.Session()\n") == [5]


def test_guard_detects_sessions_module_imported_from_requests():
    """Known-bad: ``from requests import sessions`` cannot hide Session creation."""
    assert _snippet_hits("from requests import sessions\n\n\ndef f():\n    return sessions.Session()\n") == [5]


def test_guard_detects_aliased_requests_sessions_module_import():
    """Known-bad: an alias for ``requests.sessions`` cannot hide Session creation."""
    assert _snippet_hits("import requests.sessions as rs\n\n\ndef f():\n    return rs.Session()\n") == [5]


def test_guard_accepts_bounded_requests_call():
    """Known-good: a ``timeout=`` kwarg (any expression) satisfies the guard, and
    non-HTTP ``requests`` attributes (exceptions module) are out of scope."""
    assert (
        _snippet_hits(
            "import requests\n"
            "from src.adapters.constants import ADAPTER_HTTP_TIMEOUT\n"
            "\n"
            "\n"
            "def f(url):\n"
            "    try:\n"
            "        return requests.get(url, timeout=ADAPTER_HTTP_TIMEOUT)\n"
            "    except requests.exceptions.Timeout:\n"
            "        raise\n"
        )
        == []
    )
