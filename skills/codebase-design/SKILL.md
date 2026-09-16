---
name: codebase-design
description: Use when deciding where a module boundary goes, designing a new interface, judging whether an abstraction earns its keep, or when the same logic exists in two places
---
# Codebase design

`safe-refactor` moves code without changing behaviour. This decides **where the
boundary should be** in the first place — for new code as well as old.

## Depth is the measure

A module is worth its existence when what a caller must know is much smaller
than what the module handles. Judge it by that ratio, not by line count.

- **Deep**: narrow interface, substantial hidden work. Good.
- **Shallow**: the interface costs about as much to learn as the implementation
  costs to inline. The wrapper is overhead pretending to be structure.

A one-line function with three parameters and a name that restates the body is
shallow. A 400-line module behind one function whose name says what it does is
deep, and its length is not the problem.

## The deletion test

Ask: if this abstraction were deleted and inlined, what would callers now need
to know that they currently do not?

- **Nothing** — the abstraction is not carrying anything; delete it.
- **Something specific and unpleasant** — that is what it is for; keep it and
  make sure the name says so.

## Duplication is a design signal, not a cleanup chore

The same logic in two places is not primarily a tidiness problem. It is a
prediction that a future change will be applied to one copy and reported as
complete. This repository has that shape today: `_run_managed` and `run()` are
two main loops with overlapping bodies, and a fix landed in one of them on
2026-08-31 read as done while the other path was untouched.

Before removing duplication, decide which of the two it is:

- **Incidental** — the code looks alike but the two sites answer to different
  reasons to change. Unifying them couples things that should move apart. Leave
  them, and say why.
- **Genuine** — one reason to change, two edit sites. Unify, and put the
  regression test on both entry points so the next fix cannot land in one only.

## Interfaces are test surfaces

If a behaviour is hard to test without reaching inside, the boundary is in the
wrong place — that is information about the design, not a reason to reach in.
Prefer moving the seam over adding a mock that bypasses the integration.

Name a module for what a caller gets, not for how it currently works. An
implementation-shaped name has to be renamed every time the implementation
changes, so it usually is not.

## Stop conditions

Design only as far as the current change requires. Speculative boundaries for
requirements nobody has stated are the most expensive kind of wrong: they are
hard to remove because they look intentional.

Stop and ask when the boundary encodes a product decision — what a feature
means, which concept owns a rule. Those are not resolvable by reading code; see
the `domain-modeling` skill.

## Boundaries

This decides where boundaries go. It does not perform the move safely — that is
the `safe-refactor` skill — and it does not enumerate who breaks when a public
name changes, which is the `trace-change-impact` skill.
