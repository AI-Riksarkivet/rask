# 0158. Provenance is a rask service; the lineage graph is bring-your-own (2026-10-12)

**Status:** accepted. Source: owner, 2026-10-12, grilling after ADR 0157. Narrows the lineage service.

## Context

rask's lineage service holds two things in one Postgres+AGE store: provenance, the governance record of who wrote each
commit under which run from which inputs (what the catalog enforces under ADR 0156, what notifications target from,
and what erasure and protection need to know what was derived from a table); and the lineage graph, the full
OpenLineage run history, column lineage, graph queries and the UI pages built on them. Lakekeeper emits change events and
stores no lineage graph; tools built for the graph (Marquez, DataHub, OpenMetadata) consume standard OpenLineage.

## Decision

- **Provenance stays a rask service.** The lineage service shrinks to: an authenticated OpenLineage intake (a pipeline
  posts its runs with its own token; rask stamps the verified identity, so no writer can forge another's author); the
  durable commit feed (`GET /events`, the contract pipelines poll, with the forward cursor of ADR 0156) and a table-level
  edge list, both stored as Lance tables; and a forwarder that streams every event as standard OpenLineage to a
  configured graph backend (none configured, none forwarded). Governance decisions read only what rask holds.
- **The lineage graph is bring-your-own.** Postgres+AGE, the graph-query endpoints and their UI leave rask.
- Precondition, measured first: erasure and protection are fully answerable from the table-level edge list.

## Consequences

- rask runs no relational database for lineage; the estate's only Postgres is OpenFGA's, which is the platform's in
  production.
- The lakehouse zone's lineage pages lose their backend (the frontend is out of scope).
