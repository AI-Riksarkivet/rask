# 0022. UI-operability boundaries — what deliberately has NO browser surface (2026-07-23)

**Decision.** The planes-vs-UI completeness sweep (every mutating backend op vs its MFE surface) closed with
two lists. The following are **WONTFIX — no UI surface, by design**, each for the stated reason:
- **Credential vending** (`POST /management/v1/table/{id}/credentials`) — client/API-only: the browser talks through
  the BFF and must never receive S3 credentials.
- **Bare namespace create** (`POST /v1/namespace/{id}/create`) — the warehouse-**bind** flow
  (`POST /v1/warehouses/{id}/namespaces`, in WarehouseAdmin) is the governed creation path; a second,
  unbound create surface would fork it.
- **Client-direct write protocol steps** (commit, version create/delete, batch-create/commit, alter
  transaction) — internal steps of the SDK/tooling write lifecycle (#28); a browser session never holds
  staged fragments or an open transaction. Version reclamation stays governed via maintenance
  preview/run with tag-pin protection — a raw version-delete button would bypass that framing.
- **`merge_insert` / create-with-data / register-external** — Arrow-IPC bulk paths and raw-URI registration
  (SSRF-adjacent) are pipeline/SDK/operator acts; the browser data surface is append-via-insert + declare.
- **Materialized-view create/refresh** — the backend is dormant (406); prior decision
  (feedback-no-speculative-features) forbids UI on unproven capability.
- **Lineage ingest / media ingest** (`POST /lineage`, `/ingest-media`) — service-identity seams
  (OpenLineage fidelity: humans never author lineage). `/ingest-media` does verify a bearer, but only
  to name the run's originator (`authorize_ingest_media` returns its subject, pinned to the configured
  project); it is synchronous and capped, and the edge's ingest door is `/api/ingest`, so it gets no
  gateway row and no browser surface.

The 10 buildable gaps the sweep found (table drop/deregister/rename + declare-empty, row update/delete +
backfill_column, a namespace-detail page reusing GrantsPanel/policy) are **tracked in task #85** — neither
silently dropped nor silently built.

**Related tooling verdicts:** **nats-surveyor** — deferred with parked task #20; it targets
multi-cluster/$SYS observation with Grafana dashboards, while this estate is single-cluster on
GreptimeDB/Perses; the admin UI reads `/jsz` live and time-series would come from scraping the NATS
exporter into the existing stack. **NACK** — adopt when #20 unparks (CRD-managed streams replacing the
imperative nats-stream-job; pairs with clustering). The **official nats-io helm chart is already in use**
(vendored subchart nats-2.14.2). The JetStream admin panel is **read-only** and reaches NATS only through
an admin-gated BFF proxy — the browser never connects to NATS (same posture as the audit viewer's
GreptimeDB access).
