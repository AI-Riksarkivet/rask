# 0147. /produce asks the catalog where bronze lives (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (D6, LH-164 part 1, LH-194's compensation shape, LH-420).

## Context

`/produce` builds its location from `settings.bronze_uri` or `{root}/medallion/{ns}`
(`services/medallion/src/medallion/services/produce.py:147,158`), registers it (`:193-219`) and unwinds the record on failure
(`_unwind_registration`, `:43-78`). The stage runners and `/ingest-media` instead ask the catalog
(`ensure_stage_output`, `catalog_register.py:273`: describe, otherwise `create?mode=exist_ok`). The spec's CreateTable
takes no location (`lance_docs/ns_catalog/spec.yaml:3737-3742`).

## Decision

- `/produce` resolves its bronze table through `ensure_stage_output`. The catalog places every tier table, including
  the default lane that has no project, under the platform root.
- `MEDALLION_BRONZE_URI`, `MEDALLION_MEDIA_BRONZE_URI`, `FROM_URI` and `TO_URI` (`chart/templates/medallion.yaml`) and
  the location building in `produce.py` are removed. A describe the stage cannot answer fails the stage.

## Consequences

- A failed seed leaves an empty governed table rather than a record to unwind, so the unwind and LH-420's races go.
- `describe_table_location`'s fallback to a built path on 4xx (`catalog_register.py:375-394`) goes with it.
