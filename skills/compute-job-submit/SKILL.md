---
name: compute-job-submit
description: Use when sizing, submitting, monitoring, debugging, or recovering a batch compute job on a shared cluster
---
# Compute job submission

Treat the interactive machine as a development client and the cluster as a
separate, ephemeral execution environment. Never submit or stop a job without
explicit user authorization.

## Required sequence

1. Discover the submitter, cluster endpoint, queue names, and mount layout from
   the local scheduler config and whatever instruction files are actually
   loaded in this workspace. Do not assume a layout from another deployment.
2. State workload, entrypoint, interpreter, image, queue, GPU, CPU, memory,
   timeout, mounts, output, logs, checkpoints, and status-check method.
3. Run a representative local or compute-appropriate smoke test when feasible.
4. Immediately before an authorized submission, obtain a structured per-node
   capacity snapshot plus current queued and running demand.
5. Confirm the requested GPU, CPU, and memory coexist on one relevant node;
   aggregate free resources are insufficient evidence.
6. Submit only after the user approves unresolved cost or shape choices, then
   preserve the job ID, exact request, logs, runtime versions, and outputs.

## Hard invariants

- Call the submitter the way this deployment expects it. Wrappers usually exist
  for a reason — a cleared proxy, a pinned config path — and bypassing them
  reintroduces the failure they were written to prevent.
- Scratch directories and launch scripts must live on a volume mounted at the
  same absolute path in both the client and the job. This is the classic
  failure: the job dies instantly on a missing file it was told to run.
- Pass the job a clean environment. Inherited proxy, PATH, or CUDA variables
  are a common source of jobs that run locally and die on the cluster.
- Confirm interconnect names and multi-node behavior instead of copying another
  job's configuration. Multi-node execution is not established by a single-node
  run.
- Respect shared storage that belongs to other people: mount what the job
  needs, but never stage your working set there.
- A current fit does not predict queue priority. A quiet job is not necessarily
  stalled; judge progress from state, logs, artifacts, and resource evidence.
- Persist useful logs to durable storage, because job pods and platform records
  may disappear after termination.

## Boundaries

This plans and submits **one job**: sizing it, checking it fits, and watching
it run. Estimating the resources it needs is step 2 here, not a separate
exercise.

- What a *past* run actually measured: the `experiment-log` skill. Sizing a
  new job is a legitimate reason to read it — check the run ledger **before**
  estimating runtime or VRAM. What this skill owns instead is what fits
  **right now**: history says what a job cost, a live capacity snapshot says
  whether it can be scheduled.
- Deciding whether the experiment itself is sound: the `experiment-rigor`
  skill.
