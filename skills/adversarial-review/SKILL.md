---
name: adversarial-review
description: Use when challenging evidence, assumptions, conclusions, edge cases, or a research review
---
# Adversarial review

Act as an independent opposing reviewer, not another implementation worker and
not a second voice repeating the leading hypothesis.

## Review method

1. Freeze the exact claim, decision, evidence set, and acceptance criterion.
2. List hidden assumptions and identify which one would most change the result.
3. Search for counterexamples, denominator errors, missing strata, alternative
   mechanisms, confounders, and evidence that should exist but is absent.
4. Design a discriminating check for each high-impact challenge. Prefer checks
   capable of falsifying the favored explanation.
5. Separate confirmed defects from unresolved risks and plausible speculation.

## Independence rules

- Do not edit canonical data or the implementation under review.
- Do not inherit another reviewer's conclusion as evidence. Inspect the source
  or independently reproduce the observation.
- Do not equate agreement among same-prompt agents with independent validation.
- In a workflow, return a compact challenge ledger: claim, counter-hypothesis,
  evidence, discriminating test, confidence, and impact if true.

The `research-review` recipe schedules multiple seats; this skill guides one
adversarial seat. For concrete code defects, also read
the `code-review` skill.


## Boundaries

This attacks **evidence and reasoning** — is the claim supported, is the
measurement sound, what would falsify it. It does not read a diff line by line;
that is the `code-review` skill, and running both on the same diff wastes a
pass without adding a second perspective.

Domain references:

- $HOME/docs/pipeline-lessons.md, section `A9. 反向而非同向的多 agent`,
  when it exists on this deployment — it is the maintainer's notebook,
  not a shipped file, so a missing copy is normal and not a blocker.
- core/recipes.py, builtin `research-review`
