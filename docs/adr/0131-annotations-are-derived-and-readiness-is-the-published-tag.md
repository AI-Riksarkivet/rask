# 0131. Annotations are derived, and readiness is the `published` tag (ingest 3/4, 2026-08-07)

Source: `docs/architecture/ingest-and-tier-movement.md` (decisions 3 and 4, "What landed" and "The cross-cutting
rules worth keeping"), ruled in `open_ingest_design.md` on 2026-08-07, migrated 2026-08-22.

## Context

Two questions arrived together: where an annotator's output belongs in the medallion, and how a downstream tier knows
an upstream table is ready to be consumed.

## Decision

- **Annotations are DERIVED data, so their tier is silver.** A label is produced from bronze, not ingested from the
  outside world.
- **Readiness is the `published` tag** on the table (`services/catalog/src/catalog/services/publication.py:73`). A
  publication carries the `{from_version, to_version}` range a consumer works on, so no consumer keeps a bookmark —
  delta bookkeeping is data, not state (the same rule as
  [0128](0128-incremental-ingest-is-an-anti-join-against-bronze-on-a-cron.md)).
- **Tenancy is resolved at the door.** The annotator publishes into its project's own silver namespace through
  `warehouse_registry.namespace_for`, resolved before authorization so the gate checks the object the write lands in
  (`services/annotator/src/annotator/projects/project_actor.py:318-322`; `projects/lakehouse.py:42`).

## Consequences

- Before the tenancy fix, the annotator published every tenant's labels into one bare `silver` namespace.
- Which trigger drives the cascade is a separate decision, not this one: the publication head and the bronze-write
  head both fire, by design ([0132](0132-the-two-cascade-heads-are-distinct-events-and-both-fire.md)).
- **Create-on-parent is the authorization rule** wherever a door exists for it: `register`, the catalog's create
  doors, and the medallion promotion decision. Two doors the plan named (a watch/schedule door and a table-level
  promote door) do not exist, so the rule is load-bearing at fewer sites than the plan claimed.
- **One irreversible operation gets exactly one door**: promotion is refused on the medallion producer for the same
  reason overwrite is refused as an ingest mode ([0127](0127-an-existing-lance-table-enters-by-fragment-append-or-the.md)).
