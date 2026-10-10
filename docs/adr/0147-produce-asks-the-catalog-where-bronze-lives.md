# 0147. /produce asks the catalog where bronze lives (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (D6, LH-164 part 1, LH-194's compensation shape, LH-420); the
DeclareTable shape follows from the lance_docs check the same day.

## Context

In the namespace spec the implementation, not the writer, decides where a table lives. On CreateTable "the table
location and any credential vending behavior are determined by the implementation and returned in the response"
(`lance_docs/ns_catalog/spec.yaml:3739-3742`); on DeclareTable the location is optional and, if absent, "the namespace
implementation should determine the table location" (`spec.yaml:3823-3827`). The namespace guide recommends DeclareTable
followed by a Lance SDK write over CreateTable, because CreateTable is a data operation rather than pure metadata
(`lance_docs/ns_catalog/namespace/operations/index.md:133-159`). A declared table with no data yet is reported as
`is_only_declared` (`spec.yaml:2926-2936`), but only when the caller sends the `check_declared` flag
(`spec.yaml:2828-2836`); otherwise the field is null. ListTables includes declared-only tables by default
(`include_declared`, `spec.yaml:2758`), and DeclareTableResponse carries `managed_versioning`, which the writer must
honour (`spec.yaml:3871`).

`/produce` builds its location from `settings.bronze_uri` or `{root}/medallion/{ns}`
(`services/medallion/src/medallion/services/produce.py:147,158`), registers it (`:193-219`) and unwinds the record on failure
(`_unwind_registration`, `:43-78`). The stage runners and `/ingest-media` instead ask the catalog
(`ensure_stage_output`, `catalog_register.py:273`: describe, otherwise `create?mode=exist_ok`). A producer that builds
its own path bypasses the catalog's placement and its vending scope.

## Decision

- `/produce` declares its bronze table through the catalog (DeclareTable, no location) and writes it with Lance at the
  location the catalog returns. The catalog places every tier table, including the default lane that has no project,
  under the platform root.
- `MEDALLION_BRONZE_URI` (`chart/templates/medallion.yaml:327`), `MEDALLION_MEDIA_BRONZE_URI` (`:353`),
  `MEDALLION_FROM_URI` and `MEDALLION_TO_URI` (`:584-585`) and the location building in `produce.py` are removed. A
  describe the stage cannot answer fails the stage.

## Consequences

- A failed seed leaves a declared-only table rather than a record to unwind, so the unwind and LH-420's races go.
- The fallbacks to a built path go with it. `describe_table_location` (`catalog_register.py:375-394`) only returns
  `None` on a 4xx; the built path is composed by its callers (`services/ingest_trigger.py:275-279`, `transform.py:643`,
  `api/rerun.py:195`, `services/train.py:129`).
- A failed seed's declared-only table appears in ListTables, so readers that list tiers must tolerate one.
- Whether `ensure_stage_output` moves to the same declare-then-write shape is decided in LH-164's implementation.
