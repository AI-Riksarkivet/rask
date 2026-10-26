# 0155. The medallion is tier governance inside the catalog; the lakehouse runs no stage, no training and no engine (2026-10-12)

**Status:** accepted, amended by [ADR 0156](0156-the-audit-grilling-amends-0155.md) (the gate is a tag gate; provenance travels in the spec Context). Source: owner, 2026-10-12, three grilling rounds after LH-461. Supersedes the stage-runner half of
the medallion, ADR 0139 (training stays in the producer), ADR 0140 (the Ray adapter runs in compute, reached from the
lakehouse) and the dispatch seam CP-056 built; extends ADR 0154.

## Context

After ingest left (ADR 0154), the medallion still ran the user's pipeline for them. A stage runner per hop
(`bronze-to-silver`, `silver-to-gold`, `media-to-silver`) heard a tier change, copied the rows into the next tier
stamping `stage`, `source_rowid` and a lineage column (`medallion.services.compute.transform_stage`; the operation names
`embed_features` and `aggregate_gold` are labels, nothing is embedded or aggregated), or sent a declared transform to
Ray through the engine door; the producer planned training runs the same way. Woven into that copy were four things
that are governance: the writer's permission per tier (`can_create_table`, `can_promote`), provenance, the promotion
gate (a row-count breach held for a person) and the commit announcement.

Lance treats compaction and distributed indexing as engine work (lance-ray `docs/src/compaction.md`, Lance-Ray `compact_files`), and
the namespace specification defines no compaction operation. Lakekeeper runs none of this: its background work is
expiration, purge and log cleanup, change approval is a trait inside the catalog, and its own medallion example writes
every tier from an external job.

## Decision

- **The medallion is tier governance, enforced in the catalog's write path**, so no writer can bypass it:
  - a write into a governed tier needs that tier's permission;
  - a write into silver or gold must name its OpenLineage run and its input tables at their versions, carried in the
    commit's transaction properties; the catalog refuses one without them and emits the lineage edge itself;
  - a write that breaches the promotion gate lands on a Lance branch of the tier table, and a `can_promote` holder
    approves it on the catalog's `/promotions` door (main fast-forwards to the branch) or rejects it (the branch is
    dropped); a sweep expires stale holds; no workflow engine (this replaces LH-462's saga);
  - every committed write is announced on lineage's durable `GET /events` feed, the documented contract a user's
    pipeline polls with its own token; the NATS topics stay internal.
- **Moving data between tiers is the user's pipeline**, on their compute and orchestrator. Deleted from the lakehouse:
  `services/medallion` (stage runners, the default copy, `/bronze-arrival`, `/publication-arrival`, the cascade-lag
  reader), training (`/train`, training plans, the outcome door, the plan sweep), the engine door, the engine and task
  registries, `MEDALLION_ENGINES`, compute's `/engine/runs` adapter, and `scripts/ray_stage_job.py` /
  `ray_train_job.py`.
- **The model registry stays**: `models$<name>` are governed tables, with candidate→blessed promote on the catalog's
  validator rung.
- **Maintenance decides, it does not rewrite**: version expiry, trash purge, the orphan scan, cross-store reconcile and
  protection stay, with the policy of what needs compacting or reindexing; the rewrites themselves leave for a
  bring-your-own job.
- **Compute ships in its own chart**, outside the product chart, keeping its Ray dashboard and Serve proxies.

## Consequences

- Rows: the stage/training/engine-door deletion with tier governance moving into the catalog; the gate on Lance
  branches (LH-462 closes as superseded); the maintenance split. XC-093's Ray drill is moot and closes; a lakehouse with
  no engine is the only shape.
- Notifications about a pipeline's runs come from the OpenLineage events the user's job emits, targeted by their
  `author.sub`; the lakehouse emits none for work it no longer runs.
