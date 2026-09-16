---
name: jsonl-data-safety
description: Use when joining, transforming, filtering, rewriting, or validating JSONL datasets and outputs
---
# JSONL data safety

Preserve canonical inputs and make every derived row traceable.

## Safe workflow

1. Identify canonical inputs, schemas, record keys, counting level, and the
   authority for each field before changing code or data.
2. Measure row counts, malformed lines, missing keys, duplicate keys, and key
   intersection before a join or rewrite.
3. Write a new derived artifact with a concise semantic suffix; never modify a
   canonical JSONL in place unless the user explicitly authorizes a migration.
4. Preserve original fields and provenance. Add explicit status fields instead
   of overloading null or silently dropping rows.
5. Validate output row counts, unmatched and ambiguous rows, duplicates,
   schema failures, representative examples, and source immutability.
6. Record source path and identity, matching key, transformation version, run
   ID, and validation summary in metadata or a sidecar manifest.

Never rewrite historical `source_*` or runtime provenance just to make paths
current. A read-only archive mount stays read-only; copy the required working
set to $HOME after checking capacity.

Read $HOME/docs/pipeline-lessons.md where it exists — especially `A1`, `A4`,
`A5`, and the delivery checklist — plus whatever `Data Safety` rules the
workspace actually loads. That file is the maintainer's notebook rather than a
shipped reference; when it is absent the discipline above still stands on its
own.
