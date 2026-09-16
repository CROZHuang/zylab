---
name: experiment-rigor
description: Use when designing experiments, comparing baselines or ablations, interpreting zero results, or reporting seeds
---
# Experiment rigor

Make every comparison traceable to an execution and keep absence of evidence
separate from a measured negative result.

## Before execution

1. State the hypothesis, unit of analysis, primary endpoint, baseline, control,
   exclusion rules, and stopping rule.
2. Freeze inputs, code/config identity, model snapshot, decoding, seeds, routes,
   retries, and resource shape that can affect the result.
3. Decide which comparisons are paired and which uncertainty or repeated-run
   evidence is required.
4. Define failure sentinels that prove the measured path actually executed.

## Interpretation

- Report `not run`, `not observed`, `zero`, `failed`, and `unsupported` as
  different states.
- Compare runs only after checking that metric definitions, denominators,
  labels, inputs, and execution shapes match.
- A green self-consistency check proves conformance to the chosen rule, not the
  external correctness of that rule.
- Treat aliases, provider substitutions, and mutable model catalogs as threats
  to reproducibility; record the served identity when available.
- Preserve raw logs and artifacts and link every reported number to a run ID or
  source line.

Read $HOME/docs/EXPERIMENTS.md before citing historical runs, and
$HOME/docs/pipeline-lessons.md for recurrent validation failures —
both when they exist on this deployment. They are the maintainer's
notebooks, not shipped files; a missing copy means this pod keeps no such
record, which is a fact to report rather than a gap to fill from memory.
