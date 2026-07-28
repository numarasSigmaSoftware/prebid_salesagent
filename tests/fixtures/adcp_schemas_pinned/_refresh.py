#!/usr/bin/env python3
"""Refresh the pinned AdCP JSON-schema fixtures used by test_pydantic_schema_alignment.

Source of truth: adcontextprotocol/adcp @ commit
    04f59d2d56d3d77033162c310e99a1188e4eb419  (v3.1 cut, 2026-05-13)

Plus a small, explicitly DECLARED supplement — see ``SUPPLEMENT`` below and the
generated ``_manifest.py``. The vendored tree is the base commit; anything transplanted
from a later source is named there, with the code and the reason. Do NOT advance
``PINNED_SHA`` without actually re-running this script: the pin is a claim about every
file in this directory, and ``test_pinned_schema_provenance.py`` checks the claim against
the bytes on disk.

This commit is an INTENTIONAL, frozen reference point for AdCP 3.1 semantics. The
upstream adcp repo ships constantly and `/schemas/latest` drifts; we deliberately do
NOT track it. The commit is immutable on GitHub, so the schemas are vendored here
(committed) — the alignment test reads them offline and never fetches `/schemas/latest`.

Layout: schema `$id`/`$ref` namespace is `/schemas/<rest>`; each is written to
`<this dir>/<rest>` (so `/schemas/core/account-ref.json` -> `core/account-ref.json`).

Only the transitive `$ref` closure of the request schemas the test maps is vendored.

To refresh (e.g. to advance the pinned commit — a deliberate, reviewed change):
    uv run python tests/fixtures/adcp_schemas_pinned/_refresh.py

It reads from a local clone at ~/projects/adcp if present (faster), else GitHub raw.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from pathlib import Path

PINNED_SHA = "04f59d2d56d3d77033162c310e99a1188e4eb419"

# Entries transplanted from a LATER upstream source than PINNED_SHA, kept because
# production emits them and the recovery-conformance guard grades them against this
# fixture. Declared rather than silently folded in, so the tree never claims a
# provenance it does not have. Each is byte-faithful to the cited source.
SUPPLEMENT: dict[str, dict[str, object]] = {
    "enums/error-code.json": {
        "source_sha": "467fd93d77112baf9e094e18980119edcd3a4d07",  # tag v3.1.1
        "codes_added": ("AUTH_MISSING", "AUTH_INVALID"),
        "entries_replaced": ("AUTH_REQUIRED",),
        "reason": (
            "AdCP 3.1.1 splits AUTH_REQUIRED into AUTH_MISSING (correctable) and "
            "AUTH_INVALID (terminal); production emits both at every wire boundary. "
            "The full re-vendor of this file to v3.1.1 is tracked separately."
        ),
    },
}
REPO = "adcontextprotocol/adcp"
SRC_PREFIX = "static/schemas/source"  # repo path that backs the `/schemas/...` namespace
LOCAL_CLONE = Path.home() / "projects" / "adcp"
FIXTURE_DIR = Path(__file__).parent

# Request schemas the alignment test maps to Pydantic models, plus response schemas
# whose contract individual tests assert against (the BFS roots).
ROOTS = [
    "/schemas/media-buy/get-products-request.json",
    "/schemas/media-buy/update-media-buy-request.json",
    "/schemas/media-buy/get-media-buy-delivery-request.json",
    "/schemas/creative/sync-creatives-request.json",
    "/schemas/creative/list-creatives-request.json",
    # Response schemas grounding specific contract tests:
    "/schemas/media-buy/create-media-buy-response.json",  # test_adcp_contract F4 (valid_actions/context)
    "/schemas/account/sync-accounts-response.json",  # test_sync_response_account_contract F5 (required fields)
    "/schemas/creative/sync-creatives-response.json",  # PR1399 R3-F2 (creatives required)
    # PR1399 Plan-B: machine-complete RESPONSE_ALIGNMENTS over every implemented response model.
    "/schemas/media-buy/get-products-response.json",
    "/schemas/media-buy/update-media-buy-response.json",
    "/schemas/media-buy/get-media-buy-delivery-response.json",
    "/schemas/creative/get-creative-delivery-response.json",
    "/schemas/creative/list-creatives-response.json",
    "/schemas/creative/list-creative-formats-response.json",
    "/schemas/account/list-accounts-response.json",
    "/schemas/signals/get-signals-response.json",
    "/schemas/signals/activate-signal-response.json",
    # Standalone enum vendored for the BDD error-code guard (verify_feature_error_codes.py).
    # Not in any request/response $ref closure, so it must be listed explicitly to stay pinned.
    "/schemas/enums/error-code.json",
]


def _read_local(rel: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(LOCAL_CLONE), "show", f"{PINNED_SHA}:{SRC_PREFIX}{rel}"],
        capture_output=True,
        text=True,
    )
    return r.stdout if r.returncode == 0 else None


def _read_github(rel: str) -> str:
    url = f"https://raw.githubusercontent.com/{REPO}/{PINNED_SHA}/{SRC_PREFIX}{rel}"
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (pinned host)
        return resp.read().decode()


def fetch(ref: str) -> str:
    rel = ref[len("/schemas") :]  # "/schemas/core/x.json" -> "/core/x.json"
    return _read_local(rel) or _read_github(rel)


def _reapply_supplement() -> None:
    """Re-transplant the declared SUPPLEMENT after a refresh overwrites the base files.

    A refresh rewrites each vendored file from PINNED_SHA, which would silently drop the
    declared entries. Rather than let that happen quietly, fail loudly: the supplement is a
    reviewed decision and re-fetching it from a different commit is one too.
    """
    for rel in SUPPLEMENT:
        raise SystemExit(
            f"refresh would drop the declared supplement in {rel}. Re-apply it (or retire it "
            f"by removing the SUPPLEMENT entry once the base commit carries the entries), "
            f"then regenerate the manifest."
        )


def write_manifest() -> None:
    """Record the provenance of the tree as it now stands on disk.

    The manifest is what makes PINNED_SHA a CHECKABLE claim rather than a label: it pins the
    sha256 of every vendored file plus the base error-code vocabulary, so bumping the pin
    without re-vendoring, or hand-editing a schema in place, fails the provenance test.
    """
    import hashlib

    digests = {
        f.relative_to(FIXTURE_DIR).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(FIXTURE_DIR.rglob("*.json"))
    }
    added = tuple(SUPPLEMENT["enums/error-code.json"]["codes_added"])  # type: ignore[arg-type]
    enum = json.loads((FIXTURE_DIR / "enums/error-code.json").read_text())["enum"]
    base = tuple(c for c in enum if c not in added)

    lines = [
        '"""GENERATED by _refresh.py — do not hand-edit.',
        "",
        "The provenance record for this fixture tree: which commit it was vendored from, what",
        "was transplanted on top, and the digest of every file as committed.",
        "``test_pinned_schema_provenance.py`` reads this and compares it against the bytes on",
        "disk, so a pin bumped without a re-vendor, or a schema hand-edited in place, fails",
        "instead of passing silently.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f'PINNED_SHA = "{PINNED_SHA}"',
        "",
        "# Mirrors _refresh.SUPPLEMENT; see that module for the rationale.",
        "SUPPLEMENT: dict[str, dict[str, object]] = {",
    ]
    for rel, meta in SUPPLEMENT.items():
        lines += [
            f'    "{rel}": {{',
            f'        "source_sha": "{meta["source_sha"]}",',
            f'        "codes_added": {tuple(meta["codes_added"])!r},',  # type: ignore[arg-type]
            f'        "entries_replaced": {tuple(meta["entries_replaced"])!r},',  # type: ignore[arg-type]
            "    },",
        ]
    lines += [
        "}",
        "",
        f"# The error-code vocabulary as vendored at PINNED_SHA ({len(base)} codes), before the supplement.",
        "BASE_ERROR_CODES: frozenset[str] = frozenset({",
    ]
    lines += [f'    "{c}",' for c in base]
    lines += [
        "})",
        "",
        f"# sha256 of every vendored schema file ({len(digests)} files), relative to this directory.",
        "FILE_DIGESTS: dict[str, str] = {",
    ]
    lines += [f'    "{k}": "{v}",' for k, v in digests.items()]
    lines += ["}"]
    (FIXTURE_DIR / "_manifest.py").write_text("\n".join(lines) + "\n")
    print(f"wrote _manifest.py: {len(base)} base codes, {len(digests)} file digests")


def main() -> None:
    seen: set[str] = set()
    stack = list(ROOTS)
    written = 0
    while stack:
        ref = stack.pop().split("#")[0]
        if not ref.startswith("/schemas/") or ref in seen:
            continue
        seen.add(ref)
        body = fetch(ref)
        out = FIXTURE_DIR / ref[len("/schemas/") :]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(json.loads(body), indent=2) + "\n")
        written += 1
        stack.extend(re.findall(r'"\$ref"\s*:\s*"([^"]+)"', body))
    print(f"vendored {written} schema files from {REPO}@{PINNED_SHA[:9]} into {FIXTURE_DIR}")
    _reapply_supplement()
    write_manifest()


if __name__ == "__main__":
    main()
