---
name: quantify-first
description: Use when estimating counts, runtime, storage, throughput, or bottlenecks before making a claim
---
# Quantify first

Turn an important assumption into a small measurement before choosing a plan or
reporting a number.

## Measurement loop

1. State the claim, unit, population, time window, and decision it affects.
2. Identify the smallest representative probe that discriminates the plausible
   alternatives without mutating canonical data.
3. Record the command or method, timestamp, input identity, observed count, and
   relevant exit status.
4. Extrapolate only after measuring a sample; show the arithmetic and an
   uncertainty range rather than presenting the estimate as observed fact.
5. Re-measure if the source, route, resource limit, or workload shape changed.

## Guardrails

- Distinguish `observed`, `estimated`, `inferred`, and `not measured` in the
  result. A missing measurement is never a measured zero.
- For cross-source work, report key intersection and unmatched counts before
  interpreting business or scientific results.
- For empty search output, inspect the command exit status; an unavailable tool
  or wrong path is not evidence of no matches.
- State denominators before percentages and counting levels before totals.
- Prefer bounded samples and manifests over unbounded scans, especially on
  remote or archival storage.

Read these sources when the task needs their domain details:

Further reading, when this deployment has it — these are the maintainer's
notes, not shipped files, and are absent elsewhere:

- $HOME/.codex/AGENTS.md, section `First-Principles Problem Solving`
- $HOME/docs/pipeline-lessons.md, sections `失败分类表` and `检查清单`
- $HOME/docs/environment.md for pod-specific measurement mechanics
