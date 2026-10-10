# 0017. #38b — MV-lineage is WONTFIX (no source_tables)

**Decision.** The materialized-view path emits **no** OpenLineage, and this is WONTFIX with the current code.
Do **not** fabricate an MV lineage edge from the view's own id/output_schema — that names the OUTPUT, not its
sources, a false provenance claim.

**Rationale.** The MV receives its source only as an opaque `source_query` blob the namespace server stores
without interpreting; there is no structured list of source tables to name in a lineage event (unlike the
cascade, where the source is known from stage runner settings). Unblocking requires **either** a SQL/plan parser to
extract source tables (the repo has none) **or** an API/contract change adding a structured
`source_tables: list[str]` alongside `source_query`. Parked until an MV consumer needs it. The governance
half is already done: `create_materialized_view` seeds FGA ownership on the `materialized_view` type.
