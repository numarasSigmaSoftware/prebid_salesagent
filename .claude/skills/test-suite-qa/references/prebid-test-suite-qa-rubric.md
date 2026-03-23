# Prebid Test Suite QA Rubric

Use this reference after identifying the latest changed tests on the branch.
The primary job is to review the newest changes, not to re-audit the whole
history.

## 1. Changed-scope discovery

Preferred commands:

```bash
uv run python .claude/skills/test-suite-qa/scripts/list_changed_tests.py
git diff --name-status "$(git merge-base origin/main HEAD)"..HEAD
git diff -- tests/
git diff -- src/
```

Questions:

- Which tests are new or modified on this branch?
- Which production files changed alongside them?
- Which test claims appear to cover the changed logic?

## 2. Meaningful-test checklist

Reject or flag a changed test if any of these are true:

- It never calls production code
- It mainly asserts built-in language truths or trivial local state
- It can pass while the real changed behavior is broken
- It relies on mocks that predetermine the answer
- It verifies implementation trivia instead of user-visible or contract-visible behavior

Strong signals:

- Calls a real route, `_impl`, repository method, schema method, or workflow
- Asserts behavior that would change if the bug returned
- Includes a negative case for auth or security gates
- Uses fixtures/factories rather than hand-wired test-only truth tables

## 3. Security regression checklist

Apply this when the branch touches auth, authorization, request signing,
tenant isolation, webhook validation, secrets handling, or other trust
boundaries.

Required checks:

- Identify the exact changed decision point
- Verify at least one changed test reaches that decision point through real code
- Verify there is at least one denial or rejection path
- Ask: "If I revert the fix, does this test fail?"

Red flags:

- Test only asserts `True`/`False` combinations
- Test only patches the guard helper and asserts the patched result
- Test never exercises the transport or business boundary where the bug lived

## 4. Structural guard alignment

Read `docs/development/structural-guards.md` and check whether the latest code
change interacts with:

- transport-agnostic `_impl` behavior
- boundary completeness
- repository pattern
- schema inheritance
- query type safety
- obligation coverage or test quality guards

If the branch adds code that resembles an existing guard, call that out even if
there is no failure yet.

## 5. Patterns-reference alignment

Then review `docs/development/patterns-reference.md`.
If it is missing locally, use:

- [patterns-reference.md on GitHub](https://github.com/prebid/salesagent/blob/main/docs/development/patterns-reference.md)

Compare the changed implementation against the referenced patterns after the
security review. Focus on the newest branch code, not broad historical cleanup.

## 6. Simplification prompts

Ask these after correctness:

- Can this test cover the same risk with less mock setup?
- Can two nearly identical tests be merged?
- Is there duplicated assertion logic that should become a helper?
- Would simplification hide an important security distinction?

If the answer is unclear, ask the user before making the call.
