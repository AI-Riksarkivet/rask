# rask as a full-fledged Lance-only lakehouse — state, coordination, the Lakekeeper gap, the Lance rules, and what LanceDB's own reference teaches

Written 2026-09-02 against the working tree at `feec956` (local `main`). Three parallel workflows (41 agents, ~8.8M tokens, ~1,600 tool calls) plus my own reads of `lance_docs/` (the namespace spec, the transaction spec, table format, blob v2, distributed write, performance guide, maintenance), the Lakekeeper source and the robotics repository. Every claim carries a `file:line`. Where a claim went through an adversarial verifier the verdict is stated; where verification is still running it is marked **(unverified)**.

Decisions taken as given from you: platform teams bring their own engine; the annotator is a client application; the public API is the Lance Namespace REST spec verbatim; Lance-only, permanently.

**No code was changed.**

---

## 1. The answers, in one screen

| question | answer |
| --- | --- |
| Does the lakehouse use state? | Yes, in three places, and almost none of it is actor-shaped: Lance datasets in the object store (arbitrated by the format's own put-if-not-exists), JSON registries on the control root (three with conditional writes, eight without), and Postgres (AGE lineage, OpenFGA tuples, and the Dapr state store used only for workflow history and per-user UI documents). §3. |
| Do we need distributed actors? | **No.** No core service registers one. Every coordination need bottoms out in a store an actor could not own, and each of the thirteen needs maps to a conditional write the estate already has a primitive for. §4. |
| What about the four workflow sites? | Three are watchers over a job the object store already records; the fourth (promotion review) is a durable human wait that a CAS'd record plus a NATS 2.14 scheduled message reproduces. All four are coverable by events plus a BYO engine. §5. |
| The biggest structural finding | The Lance spec already defines the governed-commit mechanism the BYO thesis needs (managed versioning, `CreateTableVersion`, `BatchCommitTables`) and rask has it half-wired: mounted, FGA-gated, but never advertised and carrying no lineage or gate. rask's real governed door is a non-spec extension. §6. |
| What Lakekeeper has that we lack | For data, nothing: a Lance-native rask table exceeds a Lakekeeper generic table on every axis. The gaps are operational and cluster in three places: conditional writes, background work (leases, attempts, a proven purge), and per-warehouse storage profiles. 36 have / 38 partial / 10 missing / 7 n/a. §7. |
| What the robotics repo teaches | It validates the core/plugin thesis in prose and refutes it in its own code. Its two artefacts worth adopting are the plan document with stable action ids and an idempotent outcome door. It is also a catalogue of what not to do. §8. |
| Where we deviate from Lance's own guidance | ~52 flagged deviations, most minor; the ones that matter: `defer_index_remap` is refused on stable-row-id datasets so compaction still conflicts with index builds; the catalog opens datasets without a shared Session; `LANCE_IO_THREADS`/`LANCE_CPU_THREADS`/HTTP timeouts unset; update/delete commit conflicts unclassified; schema changes not quiesced. §9. |
| Q4 (raw pointers) | Lance answers it natively: blob v2 external-URI values with position and size against registered base paths. A governed Lance table can reference archival bytes in place. No second table kind. §10. |
| Dapr under this plan | Workflow and actors leave the core. Pub/sub, secrets, bindings and invocation are seams with non-Dapr siblings already in the tree. The one thing without an in-repo replacement is service-to-service security. §12. |

---

## 2. Scope and method

In scope: `catalog`, `lineage`, `maintenance`, `medallion`, `ingest`, `compute`, `controlplane`, `search`, `viewer`, and the shared libraries `service-kit/lakehouse`, `service-kit/lancekit`, `lineage-kit`, `ray-kit`, `ratch`, `storage`. Out of scope by your ruling: `annotator`, `notifications`, `flows`, the frontend.

| workflow | agents | what it did | verification |
| --- | --- | --- | --- |
| state + Lakekeeper diff | 8 rask sweeps, 3 Lakekeeper inventories, 2 syntheses, 13 verifiers | state inventory, actor and workflow verdicts, coordination plan, gap matrix | done: 12 of 13 claims stand, 1 refuted (§13) |
| robotics lessons | 6 lenses, 1 synthesis, 8 verifiers | comparative read against LanceDB's reference | done: 7 of 8 claims stand, 1 refuted |
| Lance docs + conformance | 7 readers, 4 op-by-op conformance shards, 2 syntheses, 11 verifiers | best-practice extraction; 54-op spec conformance | done: 11 of 11 claims confirmed (2 tightened); full write-up in `lance-conformance-and-build-rules.md` |
| 0.12.0 + current-docs delta | read directly (no agents) | lance-namespace 0.9.0→0.12.0 spec diff, lance main docs vs vendored, pylance v10.0.0 bundled client, lance-context, lance-spark, lance-ray | done: `lance-conformance-and-build-rules.md` §9 |

Three things I read myself before trusting any agent: the Lance Namespace spec (operations, errors, REST routes, the basic-eight rule), the transaction and external-manifest-store specs, and the robotics repo's architecture and OSS-core documents.

---

## 3. Does the lakehouse use state, and where

Consolidated inventory across the in-scope units. "cc" is the concurrency control actually in the code.

| kind | store | count | cc | owners | note |
| --- | --- | ---: | --- | --- | --- |
| Lance datasets | object store | 10 | Lance commit CAS (put-if-not-exists of `_versions/N.manifest`; Append⊥Append rebases) | catalog, medallion, ingest, compute | The catalog is **not** the sole committer: `medallion/services/compute.py:229`, `ingest/lander.py:159` and `scripts/ray_stage_job.py` commit with pylance directly |
| registries, conditional | object store | 3 | create-if-absent (`If-None-Match: *` → 412) + etag CAS (`mutate_json`, 5 rounds) | catalog | `_projects/`, `_warehouses/`, `_warehouses/bindings/`; pinned by `tests/unit/test_registry_writes_are_conditional.py`; proven on RustFS |
| registries, plain | object store | 8 | last-writer-wins | catalog, maintenance, ingest | trash, protection, policies, gates, transforms, ingest staging manifests — the CAS seam sits unused in the same package |
| outbox | object store | 1 | per-key overwrite; consumer idempotent on uuid5 run id | medallion (writer), lineage (relay) | catalog and maintenance publish **bare**; pinned as debt in `test_invariants.py:196-212` |
| in-dataset refs | object store | 8 | Lance CAS for anything on a commit; **last-writer-wins for tag moves** | catalog, medallion, maintenance | `publication.py:23-25` claims spec-level conflict detection on `published`/`blessed`; the code (`_set_tag` read-then-create-or-update, `dataplane.update_tag` unconditional) does not implement it |
| lineage graph | Postgres (AGE) | 1 | unique functional indexes + name-sorted MERGE + `pg_try_advisory_lock` on the sweep | lineage | plus `public.lineage_events` feed and the DLQ |
| FGA tuples | Postgres (OpenFGA) | 1 | one transaction per Write; **no transaction spanning the Lance mutation** | catalog, medallion, maintenance | `versions.py:129-170`: a partial seed on batch-commit strands tables a retry cannot repair |
| workflow history | Postgres (Dapr state store) | 4 | turn-based, via the engine | ingest, medallion | `ingest_run`/`chunk_run`; `stage_run`, `train_run`, `promotion_review` |
| user state | Postgres (Dapr state store) | 2 | last-writer-wins; etag on one RMW | catalog | dock layouts, attached-stores list — not on the data path |
| queues | NATS JetStream | 6 | queue-group single delivery + at-least-once + `Nats-Msg-Id` dedupe on INGEST | all | LINEAGE, CATALOG_CONTROL, MEDALLION, TRAINING, INGEST, DLQ. **Nothing uses JetStream KV or NATS 2.14 scheduled messages today** |
| Ray / k8s | Ray GCS, k8s API | 4 | create-if-absent (submission id; resourceVersion) | compute, chart | Ray GCS is not fault-tolerant in the chart, so the job registry is lost on head restart |
| process-local | memory | 22 | asyncio locks or none | everywhere | version-keyed caches self-invalidate; **three cron-tick single-flight locks are correct only at replicas=1** (maintenance `_sweep_lock` and `_reconcile_lock`, the mover `_write_lock`). The ingest run claim is a time-based CAS on an in-memory dict, and cross-replica run dedupe is the workflow engine's deterministic instance id, not a lock |
| secrets | OpenBao via Dapr | 1 | read once at boot, fail-closed | all | the only Dapr block the read plane depends on for correctness |

The Lance-native alternative the spec offers for the registry layer: Directory Catalog V2 keeps the catalog's own state as a `__manifest` Lance table with an unenforced primary key, merge-insert dedup and an atomic conflict check on insert, and the installed `DirectoryNamespace` under rask's catalog already runs it (`manifest_enabled` defaults true; `lance/namespace.py` docstring). rask's JSON registries are a hand-rolled layer beside it.

---

## 4. The actor question

**Verdict: the lakehouse+compute core needs no distributed actors and has none.** `grep` over the nine in-scope services and four libraries finds no `Actor` class and no registration; `lineage-kit`'s `LineageActorMixin` is a Ray actor mixin, not Dapr. The Dapr actor runtime appears only as the substrate under Dapr Workflow.

Every coordination need in the core bottoms out in a store an actor could not own, and in each case that store already sees writers an actor never would (movers, the lander and Ray jobs commit to Lance directly). The thirteen needs, what solves them today, and the Dapr-free primitive:

| need | today | primitive | what changes |
| --- | --- | --- | --- |
| concurrent commits to one table | Lance CAS; `/commit` classifies 400/409/503 (`dataplane.py:556-596`); run-id replay marker | keep | a typed retry-on-conflict helper so callers stop string-matching `OSError` |
| duplicate project/warehouse mint, stale re-POST | `records.create_json` / `mutate_json` conditional puts | keep | this is the reference pattern the rest of the control root should adopt |
| **`published`/`blessed` tag moves** | read-then-create-or-update, no precondition (`publication.py:131-141`, `dataplane.py:1345-1368`) | conditional put on `_refs/tags/<name>.json`, surfaced as spec `ConcurrentModification` | `create_tag`/`update_tag` gain CAS; publish/promote retry once and re-run the backwards-move check; fix the docstring |
| trash, protection, policies, gates, transforms | plain overwrite | `create_json` / `mutate_json` | route every control-root record through the conditional seam |
| **trash purge vs undrop** | check-then-act (`purge.py:256-301`, `:424-470`); ships off | make the trash record the lock: `mutate_json` `IfMatch` → `{state: purging}` before any delete; undrop refuses `purging` | `_purge_one` claims first |
| maintenance sweep single-flight | `asyncio.Lock` + `replicas: 1` (`routes.py:41-82`, `maintenance.yaml:47`; pinned by `test_invariants.py:4235`) | object-store lease (`_maintenance/sweep.lock.json` with owner+expiry, etag refresh, CAS-delete) or JetStream KV | replace the lock; parameterise replicas; take the same lease in the orphan scan |
| two writers of one tier dataset | mover `_write_lock` + `moverReplicas=1` + overwrite convergence | single-writer partitioning (keep) + Lance CAS | prefer append/merge_insert with conflict retry over overwrite where the cascade allows |
| **overlapping ingest runs double-landing** | not solved: anti-join is read-then-write (`workflow.py:954-995`); cron mints a fresh idempotency key per tick (`cron.py:100-110`) | JetStream KV `create()` on `ingest-lock/<table_id>` with TTL ≥ `max_run_hours`, or `create_json` of a lease | take the lease before the anti-join, release at `emit_terminal`; losing run answers 409 |
| duplicate run creation, replica-local run index | in-process CAS (`api.py:144-169`) + engine instance-id uniqueness | durable run record via KV `create()` or `If-None-Match` | drop `InMemoryRunStore`; the record also replaces the engine's `serialized_input` as the after-crash record |
| version GC vs live readers | a chart VALUES relation (`test_gates_a15_a18.py`) enforced by the ingest deadline; `publishing` tag pins only the gated version | **tag-as-lease**: `cleanup_old_versions` exempts tagged versions by construction | `ensure_dataset` creates `ingest-<run_id>`, `emit_terminal` deletes it; movers open upstream at the version they already record |
| lost event between commit and publish | medallion uses the outbox; catalog and maintenance publish bare | route both through `publish_lineage_with_outbox` | shrink `_KNOWN_BARE_LINEAGE` to empty; pre-flight or idempotently re-seed FGA on batch-commit |
| Ray job completion without holding an ack | Dapr Workflow monitor, 30 s timer, `continue_as_new`, 24 h | the job emits its own terminal event (train and dummy do; **`ray_stage_job.py` does not**, despite `ray_submit.py:140` saying it does) + a cron that emits FAIL for terminal-bad jobs with no event | add an emitter to the stage job; extend the compute prune tick; retire `stage_run`/`train_run` |
| human approval with a deadline | `promotion_review` workflow in the producer; `raise_workflow_event` forces app-id affinity | held-promotion record (`create_json`) + a decision door that `mutate_json`-CASes status + a NATS 2.14 scheduled message for expiry | any replica serves the door; the producer's workflow runtime and state-store scope go away |
| replica-local caches | binding cache with cross-replica eviction on the control bus; ring buffer per replica by decision | version-keyed caches (keep) + JetStream sequence as the shared cursor | only when catalog `replicas>1` is wanted |
| concurrent lineage ingests | Postgres unique index + advisory lock | keep | nothing |

None of these needs a placement service, an actor state store or turn-based mailbox semantics. All of them need a conditional write the estate already has a primitive for.

The one item I would call a genuine actor fit is the overlapping-ingest anti-join: per-table serialisation. It is still a lease, not a mailbox.

---

## 5. The workflow question, site by site

| site | durable needs actually relied on | replacement | what is lost |
| --- | --- | --- | --- |
| `ingest_run` / `chunk_run` (`workflow.py:411-728`) | activity retry; child fan-out and `when_all`; deadline timer + `when_any` (the A15 enforcement); deterministic replay pinning `RunLimits`/`DatasetHandle`/`read_version`; custom status; instance-id uniqueness as cross-replica dedupe | the run's data state already lives outside the engine (unit manifest and staging manifests on the object store, outstanding work in the INGEST stream, the commit with the replay marker, the terminal record in lineage). Re-supply: a durable run record with create-if-absent; "all chunks drained" from `consumer_info` + the staging prefix (finalize already reads storage truth, `runtime.py:511-537`); the deadline as a scheduled message; terminate as a cooperative KV flag the drain checks per fetch | nothing structural. Gained: no 4 MiB activity-result ceiling on enumeration (`workflow.py:846-923`), no actor-state-store dependency. The cooperative terminate also closes today's gap where an in-flight drain outlives terminate (`workflow.py:1200-1210`) |
| `stage_run` (`medallion/workflow.py:172-275`) | a 30 s durable timer, one poll per turn, `continue_as_new`, 24 h ceiling, then re-publish the trigger | the stage job's completion **is** its Lance commit (A13); the catalog's commit/publication event already wakes the next tier. Make `ray_stage_job.py` emit COMPLETE/FAIL; let the reconcile cron (or the compute prune tick, which already lists every job with `rask.*` metadata) emit FAIL for terminal-bad jobs | the bounded "abandoned" verdict and `ray_duration_seconds`, both recoverable from Ray's own timestamps |
| `train_run` (`workflow.py:653-697`) | same monitor shape; registered only when `quality_review_enabled`, so Ray-on/review-off never watches training | `ray_train_job.py` already emits START/RUNNING/COMPLETE/FAIL over HTTP (`:484-517`); the pre-emit death is the same cron sweep | nothing |
| `promotion_review` (`workflow.py:841-911`) | the genuine durable wait: hours to days, external human event, 72 h deadline, survives restarts, outcome persisted to lineage because history is retention-bounded | held-promotion record + CAS decision door + scheduled-message expiry; resume is the same catalog publish call with `accept_assertions` | this is the one site where the engine pays for itself today and replacing it costs real code. The state machine is small (`HELD → PROMOTED|REJECTED|EXPIRED|BLOCKED`) and `gate_decision` already separates verdict from act |

---

## 6. The commit path: what the spec defines, what rask does

This is the finding that decides whether "spec-verbatim" and "BYO workers" can both be true.

**What the spec defines.** The Lance REST catalog "exposes table version management APIs that can act as an external manifest store. When used, table commits are coordinated through the catalog before the resulting table metadata is written to storage. This enables organizations to enforce governance policies such as auditing, access control, and commit validation while still preserving the Lance table format as the authoritative source of table state" (`namespace.md:1086-1089`). The mechanism (`file_format.md:5375-5438`): the writer stages `{version}.manifest-{uuid}`; the catalog records it with `CreateTableVersion` under put-if-not-exists (`CreateTableVersionRequest` carries `manifestPath`, `manifestSize`, `eTag`, `metadata`, `namingScheme`); the writer finalises the copy; readers self-heal. `DescribeTableResponse.managedVersioning: true` tells every spec client to route commits through those ops instead of Lance's native version management. `BatchCommitTables` makes `DeclareTable` + `CreateTableVersion` + `DeleteTableVersions` + `DeregisterTable` atomic across tables at the metadata layer.

That is the Lance-native answer to "how do we govern commits from workers we do not run": FGA `can_write_data`, protection, the quality gate, lineage emission and a replay marker all run **at `CreateTableVersion`**, atomically, without the catalog touching a data byte.

**What rask does.**

- The version routes are mounted (`versions.py`: `/version/create`, `/version/batch-create`, `/batch-commit`, `/version/describe|list|delete`) and delegated to the native backend. The router's `authorize` gates them with OpenFGA: `_BATCH_PATHS` (`fga_deps.py:196`) and `_action_relation` (`fga_deps.py:222-254`) map them to `can_write_data` and declare-on-parent to `can_create_table`. I first read them as ungoverned; that was wrong on authorization.
- They carry **no lineage emit, no quality gate, no protection check and no replay marker**. Only `/batch-commit` seeds ownership after the fact, non-atomically, and cannot converge after a partial seed (`versions.py:129-170`).
- `managed_versioning` appears **nowhere** in rask (`grep` over `services/catalog`, `chart`, `service-kit`: zero hits). So `DescribeTable` never advertises it, and a spec client commits natively to the object store, bypassing the catalog.
- rask's real governed door is `POST /v1/table/{id}/commit` (`data.py:326-364`): the client writes fragments with vended creds, the catalog folds `FragmentMetadata` into a metadata-only `LanceOperation.Append` **under root creds**, classifies conflicts, honours the run-id replay marker, and emits INSERT lineage. Ingest commits through it (`catalog_service.py:238`). The spec has no such route.
- Probed live against the installed backend (`lance.namespace.DirectoryNamespace`, pylance 10.0.0, lance-namespace 0.11.0): `declare_table` works; `create_table_version` is **implemented** and enforces the four-step protocol (it refused a version whose staged manifest was absent: "Staging manifest not found at `_versions/2.manifest`"); `describe_table().managed_versioning` is `None`; and `batch_commit_tables` raises `UnsupportedOperationError`. rask's own catalog skill records the same shape: 54 of 54 routed, 47 backend-backed, and seven answering a spec-correct 501 because the dir backend stubs them: `rename_table`, `backfill_columns`, `alter_transaction`, `batch_create_table_versions`, `batch_commit_tables`, and both materialized-view ops (`.claude/skills/rask-lance-catalog/SKILL.md:18-23`).
- There is **no managed-versioning switch on the backend**: the Python wrapper exposes only `manifest_enabled` and `dir_listing_enabled` (`lance/namespace.py:282-283`). Becoming an external manifest store is therefore catalog code, not a flag: rask would serve the version ops itself (the backend's `create_table_version` already validates the staged manifest) and set `managed_versioning: true` in its own `DescribeTable` and `DeclareTable` responses, both of which carry the field in the 0.11.0 models.

**The consequence.** rask's governance is real and on the wrong door. Under your two rulings the fix is not to build a commit coordinator; it is to move the existing one: implement and advertise managed versioning, put FGA + protection + gate + lineage + replay marker on `CreateTableVersion`, and demote `/commit` to a management-API convenience or delete it. Two differences from rask's current design must be decided rather than assumed: in the spec model the **client** performs the Lance commit (the catalog arbitrates the pointer), so root-cred isolation moves from "the catalog holds the only writer creds" to "vended write creds are table-scoped and short-lived", which `vending.py` already does; and cross-tier atomic visibility via `BatchCommitTables` is **not** free today, because the dir backend stubs it. Until rask implements the batch itself over its `__manifest`, the medallion's multi-table atomicity stays what it is now: per-table commits plus tags.

---

## 7. The Lakekeeper diff (verified; §13)

Method: eight rask sweeps and three Lakekeeper inventories; every row about to be marked missing or partial was re-checked against rask code by `grep`. Lakekeeper: 17 Postgres tables, three task queues (`tabular_expiration`, `tabular_purge`, `statistics`) claimed with row locks and heartbeats, an `idempotency_record` inserted inside the mutation transaction, and a trigger-incremented `version` column for optimistic concurrency. No actors anywhere. Generic tables get identity, governance, vending, soft-delete, protection, rename, listing and 16 per-action permissions, and explicitly **no commit coordination and no schema enforcement**.

**Counts:** 36 have, 38 partial, 10 missing, 7 not applicable for Lance.

**Verdict on the headline question.** A Lance-native rask table already exceeds a Lakekeeper generic table on every axis that matters for data: the data plane, the commit door with replay, versions/tags/branches, indices, blobs, schema evolution, format-aware maintenance refusals, lineage. What a generic table has that rask's record lacks is small: an operator-curated `statistics` document, a `doc` field, and a metadata-only `version` counter on the record. The real gaps are operational and cluster in three places.

**Cluster 1: conditional writes.** Tag pointer moves, the five plain control-root record kinds, and the purge-versus-undrop race all lack the CAS the same package already provides for projects and warehouses. Lakekeeper's equivalent is `FOR UPDATE` on the `tabular` row and the trigger-incremented `version`. Rows: tag CAS (missing), record CAS (partial), soft-delete lease (partial), idempotency-key on mutation doors (partial: Lakekeeper records the key inside the mutation transaction; rask has `Idempotency-Key` on ingest only).

**Cluster 2: background work.** No lease, no attempt fencing, no task record, and a purge that ships off and is unproven against RustFS. Lakekeeper: `task`/`task_log`/`task_config`, `FOR UPDATE SKIP LOCKED` claims, per-queue heartbeat, `max_retries`, stop/cancel/run-now. Candidates that fit the no-RDBMS ruling: a JetStream work-queue stream per queue (ack-wait as the lease, max-deliver as retries) or object-store lease records; a per-record attempt counter on the trash record; lineage-derived GC pins (Lakekeeper is n/a here, but the robotics repo does it).

**Cluster 3: per-warehouse storage profiles and tenancy.** rask's warehouse record is `{id, bucket, root_uri, project, status}` on the one estate endpoint and key (`warehouses.py:103-120`). Lakekeeper: a `storage_profile` JSONB + `storage_secret_id` per warehouse, validation probes before insert, location-overlap refusal, a short-term-credential cache with single-flight and half-lifetime expiry. Rows: storage profile (partial), validation endpoint (missing), overlap check (partial: register_table and external blob bases unchecked), STC cache (missing: a Ray fan-out of N workers each calling describe hits STS N times).

**Also missing or partial, in order of consequence:** namespace move and crash-atomic rename (rask's rename is a byte copy then deregister, `dataplane.py:432-515`; a crash between leaves a duplicate); a bootstrap latch and instance-admin allowlist; a security-admin/data-admin split at project level; FGA model versioning with migration hooks; structural-tuple reconcile with a repair mode; an admission gate seam after OIDC; warehouse statistics; a catalog-owned fuzzy table search (today the UI leans on the lineage graph, which only knows tables that emitted an event); case-insensitive identifiers (a policy decision the spec leaves open); external blob byte reclamation on purge; maintenance on branched datasets (refused entirely today).

**What rask has that Lakekeeper does not** (15 items; the ones that define the product): the OpenLineage graph with column lineage and a storage-to-graph reconciler that back-fills from Lance manifests, which an Iceberg catalog cannot do; provenance in the row (`lineage` JSONB in the same commit); the medallion contract and quality gates at publication; the in-process data and read plane; object-store CAS registries with no relational catalog DB; cross-store drift reconciliation with a cleanliness gate; Lance-format-aware maintenance safety; the ingest acquisition plane; time-boxed grants; the model registry as a Lance dataset.

**The eight top gaps as falsifiable claims** are in §13 with their verdicts once the verifiers finish.

---

## 8. What the robotics repository teaches (verified: 7 of 8 claims stand)

**The thesis check.** The robotics docs draw exactly your line: core = canonical tables, bounded read/write paths, the lineage graph; "does not execute your compute, run a scheduler, or decide org policy"; auth never persisted; the rebuild loop records a plan, gates approval, and emits a dependency-ordered dispatch payload it does not run. The code draws it elsewhere. `maintain_lake` runs compaction, index refresh and `cleanup_old_versions` inline, after re-projecting the whole lineage graph in-process (verified with corrected citations: `maintenance.py:330-412`, `lineage.py:2009-2075`). The "approval-policy plugin" is a non-empty-string check on a free-form CLI `--approver` with no hook, protocol or registry anywhere in `src` (verified, high). Thirty-plus modules write the lake directly and self-report `transform_runs`. The dispatch has no return door: per-action outcomes are "not yet built". The "generic, unopinionated" core bakes `robot_id`, `site_id`, `state_vector`, `action_vector` into `observations` and creates all 53 tables unconditionally (verified; the verifier notes the doc's "generic" is scoped to policy and compute, so "contradicts" is a stretch, but the schema is domain-committed).

**Measured against it, rask is tighter on governance and looser on execution.** Tighter: governance sits at the commit, one writer, the verified author, `can_promote` as an OpenFGA rung on the destination with self-approval refused. Looser: rask's core is the executor; the "emits events, BYO engine" framing appears in no rask document.

**Adopt** (each verified or code-confirmed):

1. A dependency-ordered plan document with stable, content-addressed action ids, emitted on the control lane. rask already mints the ids (`stage_submission_id`, `run_id_for`, `cascade_id`); it never publishes the plan. Key actions by action id, not artifact id (robotics' artifact-keyed map collapses two actions on one artifact).
2. An FGA-gated, idempotent outcome door keyed on the action id (`POST /plans/{id}/actions/{action_id}/outcome`, idempotent on `external_run_ref`). This is the return path robotics never built. Together with (1) it is what turns "governed executor with a swappable compute transport" into "core emits, BYO engine" while Dapr Workflow stays the reference engine.
3. A catalog-owned cross-table snapshot object: N `(table, version)` pins, optional membership set, content-derived id, every pinned version tagged. rask has only per-table `published`/`blessed` tags (verified).
4. Lineage-derived GC pins: a version any run read or trained on is provenance and must not be reclaimable by `retention_days` alone. rask's sweep never asks lineage (verified). Tag at reference time, fail closed if lineage is unreadable.
5. Reproducibility facets (code ref, environment digest, seeds, hyperparameters, read plan) as additive OpenLineage facets with a redaction gate. rask's train job emits five facets; the catalog already accepts non-reserved facets.
6. Durable reconcile reports the purge must cite (rask computes storage drift, dangling blobs, freshness and throws them away).
7. `expires_at_millis` inside vended `storage_options` on the spec describe path (verified: `tables.py:333-339` forwards only `creds.storage_options`; the vendor computes the expiry at `vending.py:220, 283` and drops it). A pylance client of rask fails hard at STS expiry.
8. A typed outcome vocabulary for promotion (`promotion_approved/rejected/expired/blocked`) on the control lane; today the outcome is prose in a lineage FAIL `errorMessage`.
9. An `ingest` facet with enumerated/skipped/fetched/landed counts on the terminal event, so "nothing changed" is distinguishable from "the listing returned nothing".
10. Committed `/v1` JSON schemas for the `lance`/`author` facets and the control envelope, and a generated, test-gated 54-op coverage matrix replacing `docs/COVERAGE.md`'s twice-corrected hand tally.

**Adapt:** the streaming batch-UDF `add_columns` path for derive-a-column stages (in the runner, commit through the catalog); a training-grade loader as a client library over catalog contracts with stable row ids and coalesced version-pinned `take_blobs`; holds as first-class governed records the sweep consults; content-keyed index-job records with a race-safe claim; `ParentRunFacet` stored as a `CHILD_OF` edge (rask emits it on every child run and the repository never reads it; verified); composite ingest units (segmented audio, sharded Parquet); three-outcome per-unit validation facets; lineage in Lance tables is workable but only the storage decision transfers, never robotics' full-graph re-projection.

**rask does better** (verified): blob v2 with measured placement tiers and external descriptors forwarded across tiers (robotics is on the legacy `lance-encoding:blob` marker and copies bytes per tier, 100% vs 0.1%); governance at the commit; OpenLineage native both ways; graph-versus-storage reconciliation that can see a write which bypassed lineage; one Append per ingest run versus a Lance version per 1024-row flush; unit identity from the listing without transferring bytes; the opaque-payload tier contract.

**Anti-lessons:** the domain-shaped canonical schema in "generic" core; a `revision` integer checked in Python as if it were CAS (two approvers reading revision 1 both write revision 2); whole-table Python materialisation in a core that advertises bounded streaming (17,619-line `curate.py`, O(N) id-column scan per blob fetch); "best-effort, never fails" as blanket policy with no storage-side repair; copying heavy bytes into every tier; evidence pointers that resolve only inside a private tracker.

**Refuted claim, for the record:** my synthesis said robotics' positional blob zip "misattributes or crashes on a null payload". Both its paths zip `strict=True` and raise; the O(N) id-column scan is real, the consequence was overstated.

---

## 9. Where rask deviates from Lance's own guidance (verified; the full 18-row list and the rules digest are in `lance-conformance-and-build-rules.md` §5–§6)

Of ~120 practices extracted, rask follows most, several rigorously (stable row ids at every create path including `write_fragments`; fixed compact → optimize_indices → cleanup order; feature-flag whitelist read from the protobuf; tagged versions as GC exemption; blob v2 thresholds pinned at measured values). The deviations that matter:

| rule | rask today | severity |
| --- | --- | --- |
| Compaction without index remap needs either FRI (`defer_index_remap=True`) **or** stable row ids in indices | rask enables stable row ids everywhere and pylance refuses `defer_index_remap` on stable-row-id datasets ("nothing to defer"), so `optimize.py:274-277` always falls back to plain compaction: compaction and index builds still conflict, and FRI trim is moot | medium: the guide's headline conflict source is live |
| Share one `Session` per process; metadata cache 1 GiB and index cache 6 GiB are per dataset instance | maintenance does (`lance_session.py`); the catalog's `open_dataset` uses bare `lance.dataset()` per request (`namespace.py:92, 116`), minting default caches per open | medium: memory and cold caches on every request |
| `LANCE_IO_THREADS` (64 default on cloud, "128 or 256 may be needed"), `LANCE_CPU_THREADS` under Ray, HTTP client timeouts and retries | none set anywhere; OPEN-WORK H2/H3 already record it | medium |
| Classify every commit conflict as rebasable / retryable / incompatible | done on `/commit`; update/delete go through `_user_sql` (`dataplane.py:945-972`), which lets a conflict escape as 5xx | medium |
| Schema changes conflict with most writes; perform them when nothing else writes | no quiesce around add/alter/drop columns | low-medium |
| Per-base storage options must be re-supplied on read | write path passes `base_store_params`; read path passes only top-level options and asserts every base shares the catalog's endpoint | low today, blocks multi-endpoint warehouses |
| `LANCE_LOG` replaces `RUST_LOG`; `lance::file_audit` and `dataset_events` are the file-deletion audit | not set; only maintenance captures `lance::execution` and `io_events` | low |
| Feature flags 32 (`DISABLE_TRANSACTION_FILE`), 64 (data overlays, unstable) and 128 (`COVERED_INDEX_METADATA`) are defined in the current format doc; ≥256 unknown | `features.py` whitelists 1\|2\|4\|8 (+16 for root-scoped GC) and names 64; 32 and 128 are refused as unknown, which is the intended fail-closed behaviour, but the comments say the spec stops at 16 and flag 32 moves the `.txn` files rask's replay marker reads into the manifest | low today; decide before bumping pylance |
| A spec REST namespace must accept `header.<name>` context headers; the generated model pages still say `x-lance-ctx-<key>` (stale) | neither direction is implemented (no `header.*` mapping in catalog or service-kit; response `context` never populated); mitigating: the pylance 10.0.0 Rust client itself sends `context` in the JSON body, so today neither side implements the mapping | low today; measured |
| lance-ray in namespace mode (`namespace_impl="rest"` + `table_id`) is how a spec client reaches a catalog | rask uses lance-ray in URI mode only; no test constructs a Rust-backed high-level client (`RestNamespace`, lancedb `namespace_client_impl='rest'`, lance-ray rest mode) against a running catalog. The only REST proof is `tests/e2e-py/test_catalog_live.py`, which drives the generated urllib3 client through rask's own transport wrapper | **high under spec-verbatim: the goal has no test with the clients a BYO worker uses** |
| Fragment sizing: 1M rows per fragment default; more fragments for concurrent merge_insert | per-tier targets in `tiers.py` (bronze 512 rows at ~1.8 MB/row, silver 262k, gold 524k) are byte-aware and reasonable | followed |
| Blob v2 default placement is <16 KiB inline, >2 MiB dedicated | rask measured 64 KiB / 4 MiB on pylance 10 and pinned those; the doc and the binary disagree, and rask sided with the binary | followed, noted |

Constraints worth pinning in an invariant test: stable row ids cannot be enabled after creation; `Restore` is incompatible with every concurrent operation; `Append` never conflicts with `Append` so idempotency must be the application's; `merge_insert` with duplicate join keys is undefined; `drop_columns` is reversible only until compaction plus cleanup; `delete_unverified=True` is unsafe while any writer is live; branches hold file references so a rename by copy orphans every branch (rask already refuses that rename).

---

## 10. Q4, the raw pointers, answered from the format

Lance blob v2 lets one column mix inline bytes, an external URI, an external URI **slice** (`Blob.from_uri(uri, position, size)`, the packed-container case) and null, with external URIs required to fall under a registered base path unless the outside-bases bypass is set (`guide.md:274-333`). rask already implements the allowlist posture (`LANCE_EXTERNAL_BLOB_BASES`, `dataplane.py:222-233`) and forwards external descriptors across tiers, stamping the base into schema metadata because pylance cannot read registered bases back (`blobs.py:53-88`).

**Update 2026-09-02, from the current lance-ray 0.5.0 and pylance 10.0.0 docs:** the format names this choice explicitly. `write_lance(..., external_blob_mode="reference" | "ingest")`: `reference` stores the external URI under a registered base, `ingest` reads the bytes and writes them into Lance-managed storage; `initial_bases` registers the bases at create and `base_store_params` / the new `base_<id>.<key>` storage-option keys carry per-base credentials at read time. rask's ingest already picks reference when a base is registered and managed otherwise (`ingest/worker.py:212-216`); the produce contract should carry the mode as a named parameter. Details in `lance-conformance-and-build-rules.md` §9.5, §9.8.

So the answer to "should the catalog hold governed pointers to non-Lance raw sources" is: **it already can, without a second table kind.** A governed Lance table whose blob column references archival bytes in place carries FGA, lineage, protection, tags and the quality gate; the bytes never move and rask never reads, writes, compacts or vends credentials for their format. That is both the robotics repo's "archival truth reachable by pointer" and Lakekeeper's generic-table use case, expressed inside Lance. Two obligations come with it: an overlap check on external blob bases against registered locations (the vended per-table creds are prefix-scoped), and a decision on whether purge reclaims external bytes (today it deletes the dataset prefix only).

---

## 11. What a full-fledged Lance-only lakehouse needs from here

Ordered by what unblocks what. Everything below is reachable with primitives already in the tree.

**A. Make the spec's commit path the governed one.** Advertise `managed_versioning`; move FGA + protection + gate + lineage + replay marker onto `CreateTableVersion`/`BatchCommitTables`; retire or alias `/commit`; add an integration test that connects with a stock `lancedb`/`RestNamespace` client and lance-ray in namespace mode. Nothing else in this list makes "spec-verbatim" true. **Conformance scorecard (verified): 12 of 54 ops verbatim, 34 partial, 3 model-differs, 5 stub, 0 route-differs, 0 missing. With the shipped Rust-backed reference client, 5 ops are unusable today and 4 answer silently wrong; the 8 blockers and their fixes are in `lance-conformance-and-build-rules.md` §3.**

**B. Conditional writes everywhere the seam already exists.** Tag moves; the five plain record kinds; the trash record as the purge lease; `Idempotency-Key` recorded as a control-root object on the mutation doors.

**C. Leases and tag-pins instead of `replicas: 1`.** Sweep lease; ingest-run lease before the anti-join; durable run record; run tags as GC leases; retire the three `asyncio.Lock`s and their invariant.

**D. The two BYO-engine artefacts.** The plan document on the control lane; the idempotent outcome door. Then the stage job emits its own terminal event, the prune tick emits FAIL for the dead, and `stage_run`/`train_run` retire. Promotion review becomes a record + door + scheduled message.

**E. Storage profiles per warehouse.** Profile + secret ref on the warehouse record; a validation endpoint with the probe ladder; overlap checks including external blob bases; an STC cache with single-flight.

**F. Lifecycle tasks as records.** Attempt counter and last error on the trash record; a proven purge against RustFS; lineage-derived GC pins; durable reconcile reports the purge cites; index-job records with a claim; a reclaimer for abandoned staging fragments with an age floor.

**G. Governance surface.** Cross-table snapshot object; bootstrap latch and instance-admin allowlist; security/data admin split; FGA model versioning; reconcile repair mode; admission seam; warehouse statistics; catalog-owned table search.

**C′. A coordinator-free alternative for append-only landing (added 2026-09-02).** lance-context's deployment spec shows MemWAL with one shard per stable pod id (`uuid5(instance_id)`), PUT-IF-NOT-EXISTS with epoch fencing, reads unioning all shards from object storage. No lease, no read affinity, throughput scales by adding shards. It fits an append-only bronze landing, not the anti-join dedup ingest does today, and blob v2 columns read back `None` through the MemWAL scanner today, so it is an option to record, not to take yet (`lance-conformance-and-build-rules.md` §9.9).

**H. Configuration hygiene from the Lance guide.** Shared `Session` in the catalog; `LANCE_IO_THREADS`/`LANCE_CPU_THREADS` in the Ray `runtime_env`; HTTP client timeouts; `LANCE_LOG`; conflict classification on update/delete; a quiesce on schema changes; `expires_at_millis` in vended options; an in-flight blob-byte admission budget (503 + `Retry-After`) on every blob door; per-base `base_<id>.<key>` storage options on the read path; the feature-flag whitelist re-checked against bits 32/64/128.

---

## 12. Dapr under this plan

The lakehouse core after A–H uses **no actors and no workflow engine**. What remains of Dapr in the core is pub/sub (25 subscription routes across four services), secrets (one function), cron bindings, and invocation, each behind a seam with a non-Dapr sibling already in the tree and a nats-py precedent in ingest. NATS JetStream, OpenBao and the two Postgres databases (AGE, OpenFGA) stay. The Dapr state store leaves with the workflows; the catalog's per-user UI documents move to a Postgres table or a Lance table.

The one loss with no replacement in the repo is service-to-service security: Sentry mTLS, app-token delivery auth on every subscription route, and per-app-id component scopes. That is a mesh, or NATS credentials per service plus a real service credential on the HTTP doors, and it must be decided before, not after, the sidecar goes.

---

## 13. Verification verdicts

Twenty-four claims went to independent adversarial verifiers told to refute them: thirteen from the state and Lakekeeper pass (**twelve stand, one refuted**) and eleven from the conformance pass (**all eleven confirmed**, two tightened). The tightened wording is what the sections above now say.

**State claims.**

| claim | verdict | what the verifier added |
| --- | --- | --- |
| No core service registers a Dapr actor; the runtime is only Dapr Workflow's substrate | **stands** (high) | the only `register_actor` calls in the repo are annotator (`main.py:130-132`) and notifications (`lifespan.py:137,143`). The workflow substrate is hosted by three core services plus `flows` |
| `published`/`blessed` tag moves carry no CAS, contradicting `publication.py`'s docstring | **stands** (high) | it is unconditional **at every layer**: `_set_tag` (`publication.py:131-141`), `dataplane.update_tag` (`:1365-1368`), `models.promote` (`:198-204`), and lance v10.0.0's own `Tags::update` is exists-check → read → plain `object_store.put` (`refs.rs:254-281`). So the fix cannot be a pylance call; it must be an object-store conditional put on `_refs/tags/<name>.json` |
| Only projects, warehouses and bindings use conditional writes | **stands** (high) | bindings are `If-None-Match` only (immutable, no `If-Match` path). Every other control-root JSON is an unconditional `open_output_stream` |
| Every non-store-arbitrated single-writer guarantee rests on `replicas: 1` + an `asyncio.Lock` | **refuted** (high) | the ingest run claim is not a lock but a time-based CAS on an in-memory dict, and the cross-replica dedupe of a run is arbitrated by the workflow engine's deterministic `instance_id = run_id`. Corrected: **three cron-tick guards** are process-local `asyncio.Lock`s pinned to `replicas: 1` (the maintenance `_sweep_lock` and `_reconcile_lock`, the mover `_write_lock`). The ingest dedupe will need a durable run record only once the engine leaves |
| Two overlapping ingest runs can double-land the same objects | **stands** (high) | precise window: run B's `enumerate_chunks` anti-join reads bronze's `id` column before run A's single `finalize` Append lands; both Append paths auto-rebase; nothing refuses the second copy |

**Gap claims** (all eight **stand**, high confidence).

| claim | what the verifier added |
| --- | --- |
| tag pointer moves have no conflict detection | the spec's `UpdateTableTagRequest` has no expected-version field, so `ConcurrentModification` here would be a rask extension in the same spirit as the run-id marker |
| no lease or attempt-fenced task record for maintenance | the purge runs under `_reconcile_lock`, not `_sweep_lock`; the trash record carries no owner, attempt or lease field |
| no per-run GC-protecting pin | the only per-run tag is `publishing`, held for the gate's duration; no code pins the version an ingest, mover or training run reads |
| no reclaimer for orphan and abandoned fragment files | "leak forever" was overstated: Lance's own `cleanup_old_versions` reclaims **verified** garbage, but uncommitted staged fragments are unverified and rask never passes `delete_unverified`, so those do persist until someone does |
| no per-warehouse storage profile or credential | a warehouse-rooted connection reuses the estate's `namespace_properties()` with only `root` swapped (`core/namespace.py:28-36`); the invariant "one endpoint, one key" is stated in `config.py:60-62, 101-102` |
| non-registry records unconditional | none of the five modules imports `service_kit.lakehouse.records`; `migrate_policy` is an unconditional read → put → delete |
| catalog publishes lineage bare | `DaprEmitter._send` swallows every failure (`lineage_emit.py:652-665`); because reconcile only sweeps datasets the graph already knows (`reconcile.py:168-171`), a lost create/declare/register event is **unrecoverable**, while a lost write event is back-filled |
| rename not crash-atomic, namespace move absent | the Lance Namespace spec itself has no namespace-rename op, so "move namespace" would be a management-API extension |

**Conformance** (54 operations): done. 12 verbatim / 34 partial / 3 model-differs / 5 stub / 0 route-differs / 0 missing. All 6 conformance claims and all 5 deviation claims were adversarially verified and CONFIRMED; two tightened (the urllib3-client e2e test exists; the 1 GiB / 6 GiB caches are soft LRU ceilings). Full scorecard, blockers, extension list, rules digest and verdicts: `lance-conformance-and-build-rules.md`.


**Conformance and Lance-guidance claims** (the full verifier reasoning is in `lance-conformance-and-build-rules.md` §7).

| claim | verdict | what the verifier added |
| --- | --- | --- |
| Reference client cannot call count_rows or tags/list (GET vs POST, uncoded 405) | **stands** (high) | reproduced end-to-end against uvicorn: the client is the Rust `PyRestNamespace`; no GET alias in catalog or gateway; the 405 body is FastAPI's default because no `StarletteHTTPException` handler is installed |
| UpdateTableSchemaMetadata response is the wrapped envelope, unparseable by the client | **stands** (high) | request side conforms; the write commits server-side and then the client raises on the response, so a spec client sees a failure for a mutation that happened |
| Explain/Analyze plans answered as text/plain, unparseable by the Rust clients | **stands** (high) | the 0.12.0 Rust client rejects `text/plain` outright; the Python urllib3 client tolerates it, which is why rask's own e2e never noticed |
| DescribeTable never reads its body; vending unreachable by a spec client | **stands** (high) | bodies `{version:99}`, `{tag:'nope'}`, `{branch:'dev'}`, `{vend_credentials:true}` and a mismatched id all answered 200 with the vendor never invoked; the query-string forms were honoured |
| Every uncoded error body collapses to InternalError 18 in the client | **stands** (high) | also the 413 body-limit, 429 load-shed and draining 503 bodies lack `code`; bodies that carry `code` parse to the right typed error regardless of status or media type, so the media type is not the problem |
| `delimiter` query parameter ignored on every route | **stands** (high) | 0 of 153 served operations declare `delimiter`; the router-level FGA gate splits with the server delimiter too, so authorization objects are derived from a mis-parsed id |
| DescribeTable binds version/tag/vend_credentials from the query string, no `branch` | **stands** (high) | the word `branch` does not appear in tables.py at all |
| No external manifest store; `/commit` is a direct pylance commit under root creds | **stands** (high) | the dir backend is connected without `table_version_management=true`; the only put-if-not-exists seam in the estate is the control-root JSON registry, not a manifest table |
| Nothing proves a stock high-level Lance client works against rask | **stands** (medium-high) | TIGHTENED: `tests/e2e-py/test_catalog_live.py` drives the generated urllib3 client through rask's transport wrapper, and the annotator uses its TagApi; what has no test is the Rust-backed high-level clients a BYO worker uses |
| Fresh `lance.dataset()` per request, no shared `lance.Session` | **stands** (high) | TIGHTENED: ~24 request-path open sites; the 1 GiB / 6 GiB figures are soft LRU ceilings, not eager allocations; the defect is zero cross-request reuse and unbounded aggregate growth |
| Commit-conflict classification exists on one door only | **stands** (high) | code-structure facts high confidence; that a real update/delete conflict surfaces as an unmarked OSError was inferred from the marker vocabulary, not reproduced live |
