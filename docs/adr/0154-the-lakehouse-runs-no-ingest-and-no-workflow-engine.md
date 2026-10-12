# 0154. The lakehouse runs no ingest and no workflow engine; data enters through the catalog's namespace doors (2026-10-12)

Source: owner, 2026-10-12, in a grilling session after CP-056 closed ("Ofc ingest should be out"; compute "as I said
BYO and aswel workflow engine/orchestration"). Supersedes R24 of ADR 0121, ADR 0036, and ADR 0006 (Dapr Workflow for the promotion).

## Context

The lakehouse hosted two kinds of work that are compute and orchestration rather than governance:

- `services/ingest` (R24, ADR 0121): a service that lists S3 prefixes, local directories or Lance datasets, fetches the
  bytes, fans the units out over a JetStream work queue and writes bronze, each run orchestrated by a Dapr Workflow
  hosted in the service (ADR 0036), started by `POST /ingests` or a Dapr cron.
- The medallion producer's `POST /ingest-media` (reads a prefix of the warehouse bucket, writes the media bronze table
  with `lance.write_dataset` and publishes `medallion.media` itself) and `POST /produce` (writes eight synthetic rows
  straight into `bronze$events` and emits the cascade-head event).
- The medallion's quality-review promotion saga, a Dapr Workflow (`medallion/workflow.py`, `services/dapr_saga.py`)
  when `qualityReview` is on.

Criterion 3 of Phase 1 is that the lakehouse is not coupled to a workflow engine or to compute, and the owner's
standing rule is that compute and orchestration are bring-your-own.

The Lance Namespace specification (`lance_docs/ns_catalog/spec.yaml`) already defines how a client puts data into a
table: `CreateTable`, `InsertIntoTable` and `MergeInsertIntoTable` take an Arrow IPC body; `DeclareTable` with
`vend_credentials` lets a client write Lance files to storage itself and commit them; `RegisterTable` adopts an
existing dataset. rask's catalog serves all of them, and announces every committed write, which is what
`/bronze-arrival` already acts on: a bronze table written through the catalog starts the cascade with no ingest
code in the lakehouse. Lakekeeper, the reference for this estate's governance, runs no compute at all (its only
background work is three catalog-housekeeping task queues) and its own medallion example writes every tier from an
external pipeline job through vended credentials.

## Decision

- **The lakehouse's only way in is the catalog's namespace doors.** No lakehouse service fetches source bytes, lists an
  external store, or writes a governed table except through the catalog. The cascade starts from the catalog's commit
  announcement on a bronze table.
- **Ingest is a client job on bring-your-own compute**: a sealed runner (`runners/ingest`, CP-061) that keeps the
  source adapters and the incremental mode as one Ray Data pipeline, writes Lance files with vended credentials and
  commits through the catalog (`insert` / `merge_insert` are allowed for small tabular sources), emits its own
  OpenLineage run, is started by the deployer's scheduler, and authenticates as whatever identity the deployer gives it.
  The chart renders no ingest object.
- **`services/ingest`, `POST /ingest-media` and the `/produce` door are deleted** (LH-461), with the catalog features
  only ingest used (`POST /v1/outbox/credentials`, the `_ingest_staging` STS statement, the `event_stager` grant) and
  the `service-ingest` identity. A seed for development and the e2e lanes writes through the catalog's `insert`.
- **The lakehouse hosts no workflow engine** (LH-462): the promotion review becomes a durable record answered on the
  existing `/promotions/*` door and expired by a cron sweep, the shape CP-029 gave runs. After it, the
  `the-lakehouse-is-not-built-on-a-workflow-engine` import contract carries no exemption.
- The producer keeps `POST /train` (ADR 0139) and its control doors.

## Consequences

- Until CP-061 lands, data reaches bronze only from a client that speaks the namespace doors; the estate loses its
  in-cluster S3, directory and Lance-append ingest in the meantime, accepted as pre-production.
- ADRs 0127, 0128, 0129, 0130 and 0131 describe the ingest service's mechanics; they hold for the runner only where
  CP-061 re-adopts them.
- XC-093 loses its Dapr-workflow outage leg; its Ray drill and CI clause remain.
