---
name: trace-change-impact
description: Use when changing a public interface, default, persisted format, permission tier, or the range a field can hold; enumerate producers and consumers first
---
# Trace change impact

Most cross-module breakage is not caused by wrong code. It is caused by one
author widening what a value can be while another author's code still assumes
the old range. Both sides are individually correct; the seam is what breaks.

Worked example from this repository: a footer formatted `f"{ctx:,}"` while the
same line guarded the percentage with `if ctx else 0` — half-defended. Later a
different author introduced `last_ctx = ... or None`. Each change was locally
right. Together they turned one gateway 503 into a traceback.

## Enumerate before editing

For the thing being changed, list explicitly:

1. **Producers** — everywhere the value is written, including tests, fixtures,
   defaults, and migrations.
2. **Consumers** — everywhere it is read, formatted, compared, or serialised.
   Search for the field name, not just the function.
3. **Stored data** — rows, files, caches, and sessions already written under the
   old shape. They will not be re-written just because the code changed.
4. **Error and empty paths** — what each consumer does with the new value when
   the operation failed, returned nothing, or was interrupted.
5. **Restore paths** — resume, replay, compaction, checkpoints, and any code
   that reconstructs state from disk.

## Which changes need this

Widening or narrowing a type. Adding `None`, an empty string, or a new enum
member. Changing a default. Renaming a persisted key. Moving a tier from ask to
allow. Adding a state a background worker can observe mid-transition.

A change that is purely internal to one function does not need this. Say so and
move on rather than performing the ceremony.

## Stop conditions

Do not edit until every consumer found in step 2 is either confirmed safe with
the new range or included in the change. If a consumer cannot be found because
it lives outside this repository, name it as an assumption in the report.

If the same logic exists in two places — two loops, two renderers, two loaders —
the change must be made in both, or the report must say which one was left. A
fix applied to one copy reads as complete and is not.

## Evidence to keep

The producer/consumer list and the decision for each consumer. When a consumer
was deemed safe without a code change, record why; that is the claim most likely
to be wrong.

See the `test-ratchet` skill for locking the new range with a regression test.
