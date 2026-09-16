---
name: test-ratchet
description: Use when adding a feature, fixing a bug, or changing behavior; require regression tests, touched-path coverage, and passing-after evidence
---
# Test ratchet

Every behavior-changing code edit should leave the repository harder to
regress than before. Match test depth to blast radius rather than maximizing
test count.

## Workflow

1. State the externally visible behavior or invariant being changed.
2. Add or identify a focused test that fails for the original defect or absent
   feature. Record the failing-before evidence when feasible.
3. Implement the smallest coherent change.
4. Run the focused test until it passes.
5. Exercise at least one relevant error, denial, boundary, compatibility, or
   restore path.
6. Run the broader affected suite, then the repository's full suite when the
   change can cross module boundaries.
7. Run syntax/import checks and git diff --check where applicable.

## Judgment rules

- Test behavior through the real boundary being changed; avoid mocks that
  bypass the integration under review.
- For a security guard, pin both allowed and denied paths. Never weaken the
  guard merely to make a fixture pass.
- For persisted records or CLI/config changes, cover old-data backfill and
  round-trip behavior.
- For concurrency or TUI changes, cover lifecycle/cleanup and stable
  synchronization instead of sleep-based timing.
- Documentation-only or pure formatting changes may need no new test, but
  still require an honest validation statement.


## Boundaries

This is the **final regression gate**: it asks whether the change is locked in
by a test that would fail without it. It does not design the build loop, does
not review the diff line by line, and does not find the cause of a bug.

- Finding the cause first: the `diagnosing-bugs` skill.
- Deciding what else the change can break: the `trace-change-impact` skill.
- Line-level defects in the diff: the `code-review` skill.

The repository's binding development and reporting rules are in
AGENTS.md. Use its Post-Change Validation and Output
Contract sections for the final evidence and disclose every skipped check.
