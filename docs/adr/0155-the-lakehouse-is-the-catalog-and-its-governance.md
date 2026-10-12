# 0155. The lakehouse is the catalog and its governance; pipelines, training and data rewrites are the user's (2026-10-12)

Source: owner, 2026-10-12, in a grilling session that followed ADR 0154. Supersedes the dispatch parts of ADRs 0139
and 0140 (training stays in the producer; the Ray adapter runs in `compute`), the medallion cascade as rask-run stage
runners, and the CP-056 engine seam; LH-462's saga replacement is superseded by the branch hold below.

## Context

After ingest left (ADR 0154), the lakehouse still ran or dispatched compute in four places:

- **Stage runners** (`rask-bronze-to-silver`, `rask-silver-to-gold`, `rask-media-to-silver`): on a tier commit they
  copy the rows into the next tier, stamping `stage`, `source_rowid` and a lineage column, or hand a declared transform
  to Ray. The operation names `embed_features` and `aggregate_gold` are labels; the default hop computes nothing. Mixed
  into that copy is governance: the tier's write permission (`can_create_table`, `can_promote`), the promotion gate
  (row-count band, held on `/promotions`), provenance, and the catalog publish.
- **Training**: `/train` plans a run, dispatches it to an engine, and the job reports on the outcome door.
- **The engine seam** CP-056 built to dispatch both: `MEDALLION_ENGINES`, run plans, outcome doors, the plan sweep, and
  `compute`'s `/engine/runs` adapter.
- **Maintenance's rewrites**: compaction, index build and optimize run in the maintenance worker. Lance treats these as
  engine work (`lance_docs/ray.md`, Lance-Ray `compact_files` and distributed indexing); the namespace spec defines no
  compaction operation; Lakekeeper's only background work is expiration, purge and task-log cleanup.

## Decision

- **The lakehouse is the catalog and its governance, and runs or dispatches no compute.** Moving data between tiers,
  training a model and rewriting a table's files are a user's jobs on their own compute and orchestrator, writing
  through the catalog's namespace doors.
- **Tier governance lives in the catalog's write path** (`services/medallion` is deleted):
  - the tier policy: which tiers a project has and who may write each (silver, gold);
  - provenance: a commit into silver or gold names its OpenLineage run and inputs (table and version) in the commit's
    transaction properties, the catalog refuses one that does not, and emits the lineage edge itself; row-level
    provenance columns are the writer's choice;
  - the promotion gate: a write that breaches it lands on a **Lance branch** of the tier table, a `can_promote` holder
    approves on `/promotions` and the catalog fast-forwards the main branch, a rejection drops the branch, and a sweep
    expires stale holds; no workflow engine;
  - commit announcements, which a pipeline reads from lineage's durable `GET /events` feed (cursor-paged,
    FGA-filtered); the NATS topics stay internal.
- **Training leaves; the model registry stays**: `models$<name>` tables and their candidate→blessed promote on the
  catalog are governance; `/train`, training plans and `ray_train_job.py` go.
- **The engine seam is deleted**: engine and task registries, run plans, outcome doors, the plan sweep, the engine
  door and `compute`'s adapter. The import contract forbidding engine imports stays.
- **Maintenance splits**: expiry, trash purge, the orphan scan, cross-store reconcile, protection and the decision of
  what needs compacting or reindexing stay; the rewrites themselves leave as user jobs.
- **Not lakehouse components**: `compute` ships as its own release, outside the product chart; viewer, search and
  annotator are catalog clients.

## Consequences

- Rows: LH-464 (stages, training and the engine seam out; tier governance into the catalog), LH-465 (the gate on Lance
  branches), LH-466 (the maintenance split). LH-462 and XC-093 close as superseded: the lakehouse hosts no engine and no
  workflow runtime once LH-464 lands.
- A development or e2e cascade becomes a pipeline the test drives itself through the catalog's doors.
