---
name: git-workspace-safety
description: Use when staging, committing, reverting, cleaning, resolving patches, or working in a dirty Git tree
---
# Git workspace safety

Assume unexplained changes belong to the user or another active worker.

## Before editing or committing

1. Run `git status --short --branch` and inspect the exact diff for files in
   scope.
2. Separate pre-existing, concurrent, generated, and task-owned changes. Work
   around unrelated files and report overlaps before modifying them.
3. Apply focused patches; do not use reset, checkout, clean, or destructive
   recovery commands without explicit authority and exact targets.

## Commit discipline

- Stage explicit task-owned paths. Never use `git add -A`, `git add .`, or a
  broad glob in a shared or dirty worktree.
- Inspect `git diff --cached --stat` and `git diff --cached` before committing.
- Keep a commit coherent and leave unrelated modified or untracked files
  untouched.
- Do not amend, reset, rebase, or rewrite history unless the user explicitly
  requests that operation.
- After committing, report the commit hash, files included, remaining worktree
  state, and any checks not run.

Follow whichever instruction files are actually loaded for this workspace —
the repository's nearest AGENTS.md always, and $HOME/.codex/AGENTS.md when
that file exists (it is the maintainer's, and absent on other deployments).
Git history is a recoverable audit trail, not permission to overwrite user
work.
