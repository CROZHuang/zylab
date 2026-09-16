---
name: pod-environment
description: Use when installing packages, creating venvs, sizing worker counts, or diagnosing network/proxy failures inside a container or dev pod
---
# Container and pod environment

Use this only for work whose behavior depends on the machine you are running
on. Re-measure dynamic facts instead of copying dated capacity, storage, GPU,
or network snapshots into a conclusion. Every number below is a shape to
verify, never a value to quote.

## Decision path

1. Identify the destination, filesystem, runtime, and resource class involved.
2. Read the mandatory rules in scope for this workspace — whatever instruction
   files are actually loaded. Deployments differ; do not assume a layout.
3. For mechanisms and probe commands, read the smallest relevant section of the
   workspace's own environment notes when they exist. When they do not,
   **measure**: `/sys/fs/cgroup/cpu.max` for the real CPU quota,
   `torch.cuda.device_count()` for GPUs.
4. Run a narrow discriminating check before changing configuration.

## Guardrails

- Treat cgroup v2 as the CPU/memory authority; never size pools from `nproc`
  or `os.cpu_count()` inside a container. Both report the *host's* cores, so
  `n_jobs=-1` spawns far more workers than the quota can run — they share the
  same throughput and pay the context-switching. Size from the measured quota.
- No swap is the common container default: a memory spike past the limit is an
  instant OOM kill, not a slowdown. Chunk large joins and batches.
- Do not assume a GPU is attached. Verify with runtime evidence.
- Know which paths survive a pod restart. Keep persistent work on a mounted
  volume; anything on the image filesystem is discarded when the pod is
  recreated. Treat any read-only archive mount as strictly read-only, and
  never stage or overflow into a volume that belongs to someone else.
- Use persistent venvs on a mounted volume; never bypass PEP 668 with
  `--break-system-packages`. Installs into the image layer vanish on restart,
  which looks exactly like "the package broke".
- When the image ships a purpose-built framework (a vendor CUDA/PyTorch
  build), a plain venv cannot see it and a naive install will silently pull a
  generic wheel that shadows it. Use system site packages plus no-deps
  installs, then verify the import path points at the image, not the venv.
- Route by endpoint, not by service name. Where several egress routes exist,
  each blocks what the others allow, and a host that fails is usually on the
  wrong route rather than down. Re-probe when evidence disagrees.
- Inspect exit status on every empty search result: exit 1 means it ran and
  matched nothing, exit 127 means it never ran. With stderr redirected the two
  are indistinguishable, and that has produced wrong conclusions.
- Submitting or terminating cluster work requires explicit user authorization.
  Read-only capacity and status checks do not.

## Output

Separate observed facts from dated documentation and inference. Report the
exact probe, route, interpreter, path, or cgroup value that supports each
environment-sensitive claim.
