# 0107. Keep the catalog DIY, not Lakekeeper, Gravitino, Unity or DuckLake (2026-09-02)

Source: `docs/audits/lakehouse-2026-09/verdict.md:5-37` (the verdict, 2026-09-02) and
`docs/audits/lakehouse-2026-09/catalog-build-vs-buy.md:9-15,66-74,114-118` (the build-or-buy analysis, same date).
The three locks it was decided under are the owner's: the Lance table format only (no Iceberg, Delta or Parquet
tables, ever; see `CLAUDE.md` "LANCE ONLY, ALWAYS"), OpenFGA for authorization, and the OpenLineage spec for lineage.

## Context

A catalog earns its keep on three things: identity and governance of objects, coordination of commits, and the
machinery around both (tasks, retention, tenancy, HA). Under a closed, single format the middle one changes
character. Iceberg puts the commit pointer in the catalog, so an Iceberg catalog must coordinate commits; Lance puts
the commit in the object store (put-if-not-exists on the manifest) and offers the catalog a choice: stay out of the
commit path, or become an external manifest store through `CreateTableVersion`. Only a Lance-aware catalog can take
the second option.

Measured against the 54-op Lance Namespace spec and the official `lance-namespace-impls` contracts (2026-07-24):

| catalog | what it gives a Lance table | `managed_versioning` |
| --- | --- | --- |
| Unity OSS, Apache Polaris, Iceberg REST | the spec's eight-op "basic" registry floor; Lance held as an opaque pointer (Iceberg REST via a dummy-schema companion table) | `false` |
| Lakekeeper 0.13.1 | a generic table: identity, governance, vending, soft-delete, protection, 16 per-action permissions; "Commit coordination: no" | not part of the API |
| Gravitino ≥ 1.1.0 | a native Lance REST facade serving 15 of 54 ops; no versions, tags, branches, indices, transactions or data ops | `false`, hard-coded |
| DuckLake | not representable: data files are Parquet by specification | n/a |

Everything Lance is building (multi-base tables, branches tracked by root, blob v2 with four storage semantics, late
materialization) lives at the format layer, and only becomes governed if the catalog understands the format. None of
the candidates does.

## Decision

Keep rask's catalog, and keep it Lance-aware. It is the governed Lance layer, not a registry:

- The public surface is the Lance Namespace REST spec verbatim, with managed versioning advertised and governance on
  `CreateTableVersion`; everything rask-specific sits on a `/management` API in Lakekeeper's style.
- **Lakekeeper is the design reference, not a substrate.** Take its operational shapes for the backlog (task queue
  with leases and attempts, idempotency records written inside the mutation, per-warehouse storage profiles,
  trigger-incremented versions) onto object-store records and JetStream, and take its `can_undrop` /
  `can_set_protection` / `can_control_tasks` rungs into `model.fga`.
- **Gravitino** is a conformance oracle for the 15 ops it serves, nothing more. **Unity** and **DuckLake** are
  disqualified.
- Versioning belongs in the format, governed by the catalog. No catalog-level versioning (lance-git, Nessie, lakeFS):
  expose format branches and tags, governed.

The decision is conditional, and the condition is the point: a DIY catalog that only implements the registry floor
is worse than Lakekeeper, because Lakekeeper does the floor better. rask earns the DIY by doing the format-aware
governance nobody else does: governed commits on the spec path, base-aware credential vending, branch-scoped
governance, cross-dataset GC pins for clones and branches, per-base blob lifecycle policy, and lineage that carries
branch parentage and clone provenance.

## Consequences

- Putting Lakekeeper under rask was costed and refused: it would mean two hierarchies (Lakekeeper's in Postgres and
  rask's on the object store, one a drifting cache of the other), two authz models on one OpenFGA store or rask's
  model deleted (Lakekeeper's `lakekeeper_generic_table` is a near-isomorph of rask's `table`, but rask's
  `validator`/`can_promote`, time-boxed grants, `can_be_notified` and branch/tag/restore rungs have no home there),
  a Postgres for the registry (reversing "the estate needs no relational DB" for the catalog), a Rust codebase to
  extend, and Lance reaching it only through the Iceberg-REST dummy-table wrapper.
- The DIY side's honest risk is capacity, not architecture: the format-aware roadmap is a lot of format-specific
  engineering, and the spec moves. If that roadmap cannot be committed to, the only other coherent option is
  Lakekeeper for the registry plus a thin rask "governed commit and blob" service, accepting two catalogs; it was
  named and not chosen.
- OpenLineage stays DIY under every option: none of the five candidates is a lineage store.
- The operational backlog Lakekeeper ships (conditional writes, leases, task records, storage profiles, a proven
  purge) is what rask owes in exchange for owning the catalog. That is the trade.
