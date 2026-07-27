# Catalog coverage — backend-backed validation (updated 2026-06-30, post version/branch backing)

Authoritative result of a **live probe** against a real native pylance `DirectoryNamespace` (create a
namespace + table, then call every op and classify 200 vs 501). Routes are 100% wired (54/54 spec ops);
this measures which are **backend-backed (200)** vs **spec-correct 501** because the native Rust backend
genuinely stubs them. Dispatch: most ops go to `native` (the Rust `DirectoryNamespace`); several go to the
in-process `dataplane` (pylance, always 200) — see `services/catalog/services/{native,dataplane}.py`.

**Tally: 47 / 54 backed (200), 7 spec-correct 501.** `uv run pytest tests/unit tests/integration` → 568 passed (measured 2026-07-12; e2e suites skipped unless their live backends are set).

> **Correction (2026-06-30):** an earlier version of this doc reported 41/54 and listed version + branch
> ops as "upstream-blocked / no pylance analog". That was **wrong**, and reading the Lance Namespace spec
> docs + an adversarial audit found why: (a) `describe`/`create`/`batch-delete` table versions were **fake
> 501s** — those three native bindings are typed `request: dict` and forward to Rust *without*
> `model_dump()`, so passing the pydantic model raised a marshalling `TypeError` that a too-broad
> `"is not an instance"` stub-hint laundered into a 501; (b) **branches** are a real native 501, but
> `lance.LanceDataset` implements Git-like branches, so they back in-process via the dataplane like tags.
> Six ops moved 501 → 200; the hint was narrowed so a real marshalling bug surfaces as 500, not a fake 501.

| Group | Backed (200) | 501 (native stub) |
|-------|--------------|-------------------|
| Namespaces (5) | all 5 | — |
| Tables lifecycle (10) | 9 | `rename_table` |
| Data CRUD (9) | all 9 (`update`/`delete` via dataplane) | — |
| Columns (6) | 5 (add/alter/drop/update-field-metadata/schema-metadata via dataplane) | `backfill_columns` |
| Indices (5) | all 5 | — |
| Tags (5) | all 5 (dataplane) | — |
| Versions (6) | 4 (`list` / `describe` / `create` / `delete`, native + dict marshalling) | `batch-create` / `batch-commit` |
| Branches (3) | all 3 (dataplane: `ds.branches` / `ds.create_branch`) | — |
| Transactions (2) | 1 (`describe`) | `alter_transaction` |
| Materialized views (2) | — | `create` / `refresh` |
| Credentials + stats (2+) | all | — |

**Beyond-spec extensions** (ours, not counted in the 54): `POST /v1/table/{id}/credentials` (scoped
credential vending) and `GET /v1/table/{id}/blobs` (credential-less blob serving with RFC 9110 Range +
ETag/If-Range, 2026-07-12) — both governed by the same router-level authorize (reader tier).

The **7 remaining 501s are genuine native-backend stubs**, not catalog gaps:
- **`create_materialized_view` / `refresh_materialized_view`** — pylance ships the *complete* typed MV API
  (request/response models with `source_query` / `output_schema` / `udtf_spec` / `auto_refresh`) and wires
  delegation on `DirectoryNamespace`, but the native dir backend raises `NotImplementedError`. A real
  implementation is a **greenfield materialization subsystem** (run a query engine → write a Lance table →
  incremental refresh); pylance offers only scan/filter, no query engine. So it's buildable, but a new
  subsystem, not a thin delegation — out of scope here.
- **`batch_create_table_versions` / `batch_commit_tables`** — external-manifest-store *batch* registration
  primitives the dir backend doesn't implement (it reads `_versions/` directly rather than acting as an
  external manifest store). A REST/managed backend would back these.
- **`rename_table` / `backfill_columns` / `alter_transaction`** — stubbed in the native Rust namespace.
  (`rename_table` 501s at the native call, so its FGA-revoke wiring is defensive — it only runs if rename
  ever succeeds.)

So the catalog is complete **to the limit of its backend**; the remaining gaps need upstream work in the
Rust `DirectoryNamespace` (rename/backfill/transaction/batch-version) or a real query engine (MV) — not a
thin in-process fill.

## Durable-artifact + recovery
- **The medallion movers write provenance by default, and real Lance data when `MEDALLION_COMPUTE_ENABLED`.**
  Off (default): they emit OpenLineage only. On: the in-process **fake-Ray compute** (`compute.transform_stage`)
  does a real `lance.write_dataset` per stage (incl. gold) — proven by `test_medallion_cascade.py`. What
  stays demo-only is the **gold whole-history JSONB embed** (`medallion_demo.py: write_gold`); the fake-Ray
  `compute.py` only stamps a `stage` column, not the JSONB. The genuine remaining gap is the **distributed**
  producer — **lance-ray** (a Ray Data job; `lr.write_lance`/`read_lance`, the same read→transform→write→
  version contract the fake-Ray compute fills) — which lands when this merges into rask's KubeRay cluster
  (`docs/DECISIONS.md`). So: event-driven + provenance + (opt-in) in-process data are validated here; the
  distributed compute + the gold-JSONB-in-the-deployed-path are the rask integration.
- **Stage recovery works** — `restore_table` (v1→v3 verified) on the native backend; the medallion `WROTE`
  edge records the Lance version (`DatasetVersionDatasetFacet`); `/reconcile` anchors gold to the on-disk
  `latest_write_version`.
- **Retention nuance:** the compaction cron runs `cleanup_old_versions(older_than=7d)` over **every** dataset
  (gold included). It always keeps the **current** version (no data loss), but caps **time-travel depth** at
  7 days uniformly — there is no per-stage retention. If gold needs a longer recovery window than
  intermediate stages, make `olderThanDays` stage-aware. (Not a durability violation — the system-of-record
  is preserved; only old version *history* is GC'd.)

## Low coupling (the rask-mergeability bar) — PASS
Zero cross-service imports; `common` imports no service; services share only the cross-cutting lib
(`fga`/`secrets`/`dapr_auth`/`oidc`/`exceptions`) and talk only over Dapr/NATS + HTTP. The lakehouse +
event-driven estate is a self-contained, contributable unit.

## Lakekeeper diff (for the ephemeral spin-up-per-workload model)
**N/A by design:** multi-warehouse data plane, control-plane management API, soft-delete/undrop (replaced by
Lance version time-travel + `restore_table`), user/role admin API, Postgres task queue (NATS+Dapr instead).
**Ahead of Lakekeeper:** the lineage moat (reconcile, column lineage, gold whole-history JSONB) +
web-identity credential vending. One caveat: a *long-lived single shared* cluster would want runtime
grant/role admin — N/A only while the model stays spin-up-per-workload.
