---
name: test-suite-qa
description: >
  Review the latest branch changes for meaningful tests that exercise real
  application behavior, especially security-sensitive paths and regression
  fixes. Use when new tests were added or modified and you need to verify they
  are not tautologies, mock-only success cases, or shallow stubs; when auth,
  security, or gate logic changed and tests must fail on regressions; when the
  branch should be checked against structural guard expectations in
  docs/development/structural-guards.md; and when recent code should be compared
  against the patterns in docs/development/patterns-reference.md on main (or
  the GitHub copy if the file is not present locally). After the review, suggest
  simplifications and ask the user for feedback if tradeoffs are non-obvious.
---

# Test Suite QA

## Overview

Audit changed tests and their related production code with a bias toward recent
branch work. Prove that the tests would catch the bug they claim to cover,
especially for security fixes, then compare the implementation against the
project's structural guards and patterns reference.

## Quick Start

1. Identify the review base.
   Prefer `origin/main`. If the user gives a different range, use that.

2. List changed tests and changed production files.
   Run:
   ```bash
   uv run python .claude/skills/test-suite-qa/scripts/list_changed_tests.py
   ```
   If needed, inspect the full diff:
   ```bash
   git diff --stat "$(git merge-base origin/main HEAD)"..HEAD
   git diff -- tests/
   git diff -- src/
   ```

3. Read the changed tests and the production code they exercise.
   Focus on newly added or modified tests first. Older tests are context, not
   the primary target.

4. Apply the quality rubric in
   `references/prebid-test-suite-qa-rubric.md`.

5. Run the smallest meaningful verification commands available.
   Use targeted `uv run pytest ...` invocations first, then broader checks if
   the change is substantial. If `uv` is unavailable in the current shell,
   fall back to the project's equivalent Python entrypoint rather than changing
   the test strategy.

6. Report findings in this order:
   - Meaningless or weak tests
   - Security regression gaps
   - Pattern or structural guard mismatches
   - Simplification opportunities
   - Open questions that need user feedback

## Core Review Questions

For each changed test, answer these questions explicitly:

1. Does the test call production code?
   A meaningful test must invoke the real function, method, route, or workflow
   that could regress. Reject tests that only check Python truths, local helper
   behavior, or mocks talking to mocks.

2. Would the test fail if the branch bug came back?
   For security fixes, the answer must be yes. If auth or gate logic changes in
   `auth.py`, a valid test must go through that gate or a boundary that
   deterministically depends on it.

3. Is the assertion tied to application behavior rather than language behavior?
   Example of weak logic:
   ```python
   assert (not True) is False
   assert (not False) is True
   ```
   These say nothing about the app. Treat them as non-tests unless they are a
   tiny sanity check inside a larger production call, which is uncommon.

4. Can mocks or stubs simulate success while the real system is broken?
   If yes, flag it. Tests should not pass purely because a mocked collaborator
   echoes the expected value.

5. Is the test scoped to the newest branch changes?
   Prioritize the latest modified tests and the code they claim to protect.
   Existing older coverage can remain untouched unless it directly hides a
   regression in the current work.

## Security Review

When the branch touches auth, permissions, tenant isolation, webhook
verification, request validation, or other trust boundaries:

1. Trace the changed security path from entry point to decision point.
2. Confirm at least one changed test hits the real decision path.
3. Confirm the negative case is covered.
4. Confirm the test would fail if the old vulnerable behavior returned.
5. If the test only asserts helper booleans or patched responses, flag it as
   insufficient.

Security tests should guard the current fix, not merely describe intent.

## Pattern Review

After test-quality review, compare the latest production changes against:

- `docs/development/structural-guards.md`
- `docs/development/patterns-reference.md` if it exists locally
- Otherwise: `https://github.com/prebid/salesagent/blob/main/docs/development/patterns-reference.md`

Use the local structural guards as the authoritative machine-checkable baseline.
Use the patterns reference to review implementation style and expected code
shape after the security check is complete.

If the patterns reference is only available on GitHub, fetch just that file and
cite specific patterns by name rather than copying it into the skill.

## Simplification Pass

After correctness review, look for simplifications that preserve behavior:

- Remove duplicated setup or assertions
- Replace brittle mock choreography with direct production calls
- Collapse redundant tests that prove the same thing
- Extract shared helpers only when the logic is truly repeated

If a simplification has non-obvious tradeoffs, ask the user for feedback before
rewriting the test strategy.

## Anti-Patterns

- Do not approve tests that only assert Python language properties
- Do not accept tests whose main proof is `mock.assert_called*`
- Do not treat a green mocked path as proof of a real security fix
- Do not spend most of the review on old unchanged tests
- Do not recommend skipping tests to get a pass
- Do not ignore structural or pattern mismatches after reviewing security

## Outputs

Produce a concise review with:

1. Findings ordered by severity, with file references
2. A short note on whether the current security fix is regression-protected
3. A short note on whether the latest implementation matches the relevant
   patterns and guards
4. Simplification suggestions
5. Questions for the user only when a tradeoff needs a decision

## Resources

- `references/prebid-test-suite-qa-rubric.md` for the detailed rubric and grep
  prompts
- `scripts/list_changed_tests.py` for branch-focused test discovery
