# 0129. Ingest needs the warehouse and namespace, never the table (ingest 1d, 2026-08-07)

Source: `docs/architecture/ingest-and-tier-movement.md` (decision 1d and "What landed"), ruled in
`open_ingest_design.md` on 2026-08-07, migrated 2026-08-22.

## Context

An ingest run lands rows into a bronze table under `project > warehouse > namespace > table`. Something has to exist
before the first run; the question is how much, and who creates the rest.

## Decision

- **The warehouse and the namespace must pre-exist. The table must not.** Ingest creates its table.
- **CREATE is the existence oracle, not a read door.** The catalog answers 403 for an absent table on both `describe`
  and `exists`, so neither can tell "absent" from "hidden" without becoming an existence oracle for table names
  (measured against the deployed catalog as `service-ingest`, 2026-08-06). Ingest therefore treats a 403 or 404 from
  `describe` as "try create" (`services/ingest/src/ingest/catalog_service.py:435-466`), and create is authorized on
  the PARENT — `can_create_table` on the namespace, the estate's create-on-parent rule — so a caller who may not
  create is refused there, naming the right object (`catalog_service.py:536-545`). Swapping `describe` for `exists`
  was considered and killed by the same measurement.
- **A missing warehouse-scoped namespace is refused, naming the three admin doors** (`POST /v1/projects`,
  `POST /v1/warehouses`, `POST /v1/warehouses/{id}/namespaces`; `catalog_service.py:510-532`). Ingest is a WRITER:
  provisioning the chain would make the data plane mint its own `project#admin` tuple.

## Consequences

- Before the "try create" fix, every ingest run that ever succeeded did so against a table someone had already
  created.
- A 403 on the namespace existence probe is reported as a missing GRANT (`can_get_metadata` on the namespace), not as
  missing tenancy, because the namespace may exist and be invisible to this identity (`catalog_service.py:488-503`,
  measured 2026-09-10).
- The namespace refusal is a cross-service contract held together by a string literal: ingest matches the catalog's
  "must belong to a warehouse" prose (`catalog_service.py:525`, emitted at `services/catalog/src/catalog/api/fga_deps.py:1174`). Both halves are pinned in
  `services/ingest/tests/test_unit_dedupe_and_namespace_refusal.py`; rewording the catalog's message silently degrades
  the actionable refusal to a generic 400.
