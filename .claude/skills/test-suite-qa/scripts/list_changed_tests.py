#!/usr/bin/env python3
"""List changed tests and related production files on the current branch."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def candidate_bases() -> list[str]:
    return ["origin/main", "main", "origin/master", "master"]


def resolve_base() -> str:
    for ref in candidate_bases():
        try:
            git("rev-parse", "--verify", ref)
            return ref
        except subprocess.CalledProcessError:
            continue
    raise SystemExit("Could not resolve a base branch from origin/main, main, origin/master, master")


def classify(path: str) -> str | None:
    pure = Path(path)
    if pure.parts and pure.parts[0] == "tests":
        return "test"
    if pure.parts and pure.parts[0] == "src":
        return "src"
    return None


def main() -> int:
    try:
        base_ref = resolve_base()
        merge_base = git("merge-base", base_ref, "HEAD")
        diff_output = git("diff", "--name-status", f"{merge_base}..HEAD")
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.strip() or str(exc), file=sys.stderr)
        return 1

    changed_tests: list[tuple[str, str]] = []
    changed_src: list[tuple[str, str]] = []

    for line in diff_output.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        bucket = classify(path)
        if bucket == "test":
            changed_tests.append((status, path))
        elif bucket == "src":
            changed_src.append((status, path))

    print(f"base_ref: {base_ref}")
    print(f"merge_base: {merge_base}")
    print()

    print("changed_tests:")
    if changed_tests:
        for status, path in changed_tests:
            print(f"  {status} {path}")
    else:
        print("  (none)")

    print()
    print("changed_src:")
    if changed_src:
        for status, path in changed_src:
            print(f"  {status} {path}")
    else:
        print("  (none)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
