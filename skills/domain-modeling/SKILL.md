---
name: domain-modeling
description: Use when a term means different things across files, when naming a new concept, or when a decision would be expensive to reverse
---
# Domain modeling

A project's vocabulary is an interface. When one word means two things, every
reader pays for it forever, and the ambiguity spreads into function names, field
names, log lines, and eventually stored data.

## When a term is contested

1. Collect the actual uses. Grep the term and read each site; do not reason from
   the name alone.
2. Decide whether it is one concept with two representations, or two concepts
   sharing a name. These need opposite fixes: unify the first, split the second.
3. Name each resulting concept so the name states what it is, not how it is
   currently implemented or where it happens to live.
4. Apply the name everywhere at once, including tests, comments, and messages
   the user sees. A half-applied rename is worse than the original ambiguity.

## Naming rules that earn their keep

- Prefer the word the users of this system already say out loud.
- Do not encode the current storage or transport in the name; those change.
- Two things that must never be confused should not differ by one character or
  by singular/plural alone.
- If a name needs a comment to disambiguate it, the name is wrong.

## Recording a decision

Write it down only when reversing it would be expensive: a persisted format, a
public interface, a security boundary, or a choice already argued once. Record
what was decided, what was rejected, and why — the rejected option is the part
that stops the argument from restarting.

Ordinary naming choices do not need a document. Adding one for every decision
buries the few that matter.

## Stop conditions

Stop and ask the user when the ambiguity is about product semantics rather than
code: what the feature should mean, which behaviour is correct, what a field is
supposed to represent. Those are not resolvable by reading the repository.

Proceed without asking when the answer is recoverable from code, tests, git
history, or a quick measurement.

Renaming across modules is a change-impact problem: see the
`trace-change-impact` skill before touching persisted keys or public names.
