---
name: code-review
description: Use when reviewing a diff, PR, or uncommitted changes; check correctness, security, compatibility, tests, and severity
---
# Code review

Review for concrete defects introduced by the change. Lead with findings, not
a walkthrough or praise. Do not report style preferences unless they hide a
real correctness or maintenance risk.

## Review loop

1. Read repository instructions and the exact diff plus nearby call sites.
2. Reconstruct changed control flow, persisted formats, permission boundaries,
   failure paths, and old-data compatibility.
3. Look for correctness, security, compatibility, tests, performance,
   operations, observability, and maintainability regressions.
4. Check whether tests exercise the changed behavior and the denied/error path;
   do not infer coverage merely from a green suite.
5. Validate high-confidence claims with the narrowest safe command available.
6. Return findings ordered by severity. If none remain, say so and list any
   verification gaps.

## Finding quality

Every finding must identify severity, confidence, path/line, concrete impact,
code evidence, and a narrow remediation. Reject a candidate finding that
cannot answer: what breaks, who is affected, and where the evidence is.

On the maintainer's pod a fuller rubric lives under
`$HOME/.codex/skills/code-quality-review/references/` (severity, finding schema,
review rubric, testing policy, security checks). **Those belong to a different
tool's installation and are usually absent** — check before citing them, and do
not treat a missing file as a missing standard. The severity ordering above is
self-contained.


## Boundaries

This reviews **a concrete diff**: correctness, security, compatibility, tests,
severity. It works line by line against code that already exists.

- Challenging a claim, an experiment, or a conclusion rather than code: the
  `adversarial-review` skill.
- Whether the change reaches callers it did not touch: the
  `trace-change-impact` skill.
- Whether a regression test exists at all: the `test-ratchet` skill.

Treat repository and environment rules as part of correctness. Never claim a
test, runtime, provider, GPU, or external integration was exercised unless the
current review actually observed it.
