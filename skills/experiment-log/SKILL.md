---
name: experiment-log
description: Use when reporting prior runs, benchmark numbers, experiment IDs, failures, or provenance evidence
---
# Experiment log

Anchor historical claims in the experiment registry and its source logs before
quoting a result.

## Boundaries

This answers **what a past run actually measured** — run ID, throughput, VRAM,
outcome, and the log that proves it. It is a lookup, not a planning aid.

- Sizing or submitting a new job: the `compute-job-submit` skill, which takes
  capacity from a live snapshot rather than from history.
- Designing the comparison or reading a zero result: the `experiment-rigor`
  skill.

## Lookup procedure

1. Read $HOME/docs/EXPERIMENTS.md if it exists, and identify the exact run ID,
   workload, configuration, and source log. It is generated from job logs and is
   authoritative when present. If it is absent, this deployment keeps no such
   ledger — say so rather than quoting a number from memory.
2. Check whether the registry snapshot is current enough for the question. It
   is generated from logs and must not be hand-edited.
3. Inspect the cited log lines or artifact when the number affects a decision;
   registry summaries are navigation, not a substitute for raw evidence.
4. Verify success from the workload's explicit sentinels and process evidence,
   not only the scheduler's terminal state for the job.
5. Report observed values with run ID, timestamp, hardware, repetitions, and
   caveats. Mark remembered or extrapolated numbers as unverified.

Do not merge results across runs with different code, inputs, metrics, context,
batch shape, model identity, or failure status. Preserve source logs and rebuild
the registry with its documented generator only when the user asks to update
it.

Primary reference: $HOME/docs/EXPERIMENTS.md when the deployment has one.
