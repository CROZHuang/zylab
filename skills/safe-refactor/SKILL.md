---
name: safe-refactor
description: Use when refactoring legacy code, extracting modules, renaming APIs, reducing duplication, or preserving behavior
---
# Safe refactor

Preserve observable behavior first, then improve structure in reviewable steps.

## Refactor loop

1. Define the behavior that must remain stable, including persisted formats,
   errors, permissions, compatibility, concurrency, and user-visible text.
2. Add or identify a characterization test that exercises the real boundary.
   Record failing-before only for an actual defect; a pure refactor should start
   from passing behavioral evidence.
3. Find the narrowest seam and change one responsibility at a time. Avoid mixing
   feature changes, renames, formatting churn, and broad mechanical rewrites.
4. Keep adapters or migrations where old callers and stored data still exist.
5. Run focused tests after each coherent step, then affected and full suites in
   proportion to the blast radius.
6. Review the final diff for accidental deletions, changed defaults, hidden I/O,
   import cycles, and user-owned worktree changes.


## Boundaries

This owns **behaviour-preserving structural change**. If behaviour is meant to
change, this is not a refactor and the discipline below does not apply.

- Public names, defaults, or persisted shapes move: read the
  `trace-change-impact` skill first; the seam you are cutting has callers.
- The test discipline: the `test-ratchet` skill.
- The final defect pass on the diff: the `code-review` skill.

Repository-specific constraints remain authoritative.
