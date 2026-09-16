---
name: diagnosing-bugs
description: Use when a failure is intermittent, unexplained, survived a first fix, or regressed performance with no known cause; reproduce before editing
---
# Diagnosing bugs

The failure mode this exists to prevent is editing code while the cause is still
unknown. A fix applied to a guess costs a second debugging session on top of the
first, and it silently changes code that was never broken.

## Before any edit

1. State the symptom as an observation, not a theory: exact command, exact
   output, and how often it happens. "Sometimes hangs" is not yet a symptom.
2. Build the tightest reproducer that still fails. Shrink inputs, remove steps,
   pin versions and seeds. Note the failure rate if it is not 1 in 1.
3. If it cannot be reproduced, say so and switch to gathering evidence — extra
   logging, a narrower assertion, a recorded transcript. Do not fix blind.

## Narrowing

- Change one variable per attempt and write down what it ruled out. An attempt
  that ruled nothing out was not an experiment.
- Prefer bisection over inspection when history is available: a first-bad commit
  beats an hour of reading.
- Distinguish the layer: input, transformation, persistence, or presentation.
  Confirm the value is already wrong where you think it is, rather than assuming.
- For intermittent failures, look for shared state, ordering, timing, resource
  limits, and retries before suspecting logic.
- For performance, measure before theorising; see the `quantify-first` skill.

## Stop conditions

Stop diagnosing and start fixing only when you can say: this input, through this
path, produces this wrong value, because of this line. If any clause is a guess,
you are not done.

Stop and report instead of continuing when the reproducer needs data, hardware,
or permissions you do not have. A blocked diagnosis reported honestly is more
useful than a plausible fix.

## Evidence to keep

The reproducer, the ruled-out hypotheses, and the observation that identified the
cause. All three belong in the report; the first belongs in a test.

Hand off to the `tdd` skill (or `test-ratchet` when that is not present) so the
reproducer becomes a failing test before the fix lands.
