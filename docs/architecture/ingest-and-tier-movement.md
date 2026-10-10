# Ingest, Lance-table sources, incremental runs, and tier movement

What the ingest plane rests on and what has landed against it. The decisions themselves live in the ADR log; this
page points at them and describes the system they produced. Implementation status lives in the root
`open_ingest_design.md` while work is in progress, because `docs/` asserts settled.

The evidence convention: `path:line` means read from source, `(measured <date>)` means observed against a running
system, and `UNVERIFIED` means an inference.

---

## The decisions

- **1b**, an existing Lance table as a source: [ADR 0127](../adr/0127-an-existing-lance-table-enters-by-fragment-append-or-the.md).
- **1c**, incremental / CDC: [ADR 0128](../adr/0128-incremental-ingest-is-an-anti-join-against-bronze-on-a-cron.md).
- **1d**, what must pre-exist: [ADR 0129](../adr/0129-ingest-needs-the-warehouse-and-namespace-never-the-table.md).
- **2**, manual push to bronze: [ADR 0130](../adr/0130-a-manual-push-to-bronze-is-an-authorization-policy-not-a.md).
- **3 / 4**, annotator output and tier movement: [ADR 0131](../adr/0131-annotations-are-derived-and-readiness-is-the-published-tag.md).
  Which trigger drives the cascade is a separate decision: [ADR 0132](../adr/0132-the-two-cascade-heads-are-distinct-events-and-both-fire.md).

---

## What landed

* **1d, the table must NOT pre-exist.** A 403 from `describe` means "try create", not "give up"; CREATE is the
  existence oracle because it is gated on the PARENT's `can_create_table`.
* **1d, the namespace refusal.** A missing warehouse-scoped namespace is refused naming the three admin doors. Both
  halves are pinned (`services/ingest/tests/test_unit_dedupe_and_namespace_refusal.py`).
* **1c, the anti-join, and its ceiling.** `RASK_INGEST_INCREMENTAL_MAX_ROWS` bounds the O(existing rows) read, and
  refuses rather than samples.
* **3, tenancy.** The annotator publishes into its tenant's own silver namespace through
  `warehouse_registry.namespace_for`, resolved at the door before authorization.
* **`DWF-MGT-003`.** `POST /v1/ingests/{run_id}/terminate` stops a live ingest run, bounded rather than instant and
  saying so.
