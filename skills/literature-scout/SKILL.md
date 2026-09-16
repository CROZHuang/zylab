---
name: literature-scout
description: Use when finding papers, documentation, benchmarks, citations, or current technical evidence
---
# Literature scout

Find source-backed evidence while making network failures and search coverage
visible.

## Search discipline

1. Translate the question into concepts, aliases, dates, versions, and explicit
   inclusion or exclusion criteria.
2. Prefer primary sources: papers, official documentation, standards, release
   notes, and first-party repositories.
3. Record query, source, publication or update date, accessed date, and the
   claim each source supports.
4. Triangulate high-impact claims across independent primary evidence when
   feasible; label inference separately.
5. Report search gaps, inaccessible sources, and failed routes rather than
   turning them into a claim that no evidence exists.

## Network routing on this pod

- Route by destination, never by the topic or model name.
- Vendor LLM APIs often sit on a different egress route than general web.
- Intranet services and package mirrors usually need direct routing.
- Hugging Face and general internet use `proxy_on`; GitHub prefers direct first.
- Select the route in the same command because shell state does not persist.
- Never put credentials in query strings, logs, prompts, or copied commands.

Before diagnosing an access failure, read whatever routing notes this
deployment has — on the maintainer's pod that is
$HOME/docs/codex-environment-chat.md plus the network section of
$HOME/.codex/AGENTS.md; elsewhere those are absent and the routes differ, so
probe rather than assume. Re-check current web facts instead of relying on
dated local notes either way.
