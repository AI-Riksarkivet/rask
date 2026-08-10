# open_batch_process.md — the bronze→silver batch plane: what is needed, and the goal

**What this is.** The working plan for the next major build: the **bronze→silver batch
transform + silver evolution**, plus the tier-movement rewiring it depends on
(`open_ingest_design.md` §4 holds the full trigger/promote analysis; its decisions are
restated here so this doc is self-sufficient). Root `open_*.md` rules apply: this file is
deleted when the work lands.

**Status of the ground under it.** The ingest plane converges (the (key, etag) identity +
enumerate anti-join landed and were proven live 2026-08-07 — bronze holds each object
exactly once per `(key, etag)`; `services/ingest/tests/test_convergence.py` pins it). The
`submission_id` collapse is fixed (`2d18976a`): the id now folds the work identity
(`from→to` URIs), so token-less submissions of a stage no longer share one Ray job id.
IIIF is dropped from ingest — sources are manual push + S3 only.

---

## 1. The owners — the design adds no fifth one

```
catalog     — owns the commit (read_version CAS + rask.ingest.run_id= marker replay)
NATS        — owns unit retry (WORK_QUEUE, ack/nak, maxDeliver)
Ray         — owns heavy compute (lance_ray read/write, actor fan-out, scheduling)
maintenance — owns table health (compaction, optimize, cleanup, refusal gates)
Dapr WF     — owns durable COORDINATION: submit → durable wait → react. Nothing else.
```

The transform is a **coordinator + a job**. Dapr Workflow coordinates (worth-it-narrowly:
durable waves, replay, one terminal step); Ray computes; lance-ray is the designated seam
for heavy transforms and, later, distributed compaction (maintenance stays the one owner;
its refusal gates — feature flags, branches, base_paths, blob sidecars — ride along
unchanged).

---

## 2. Invariants the build must carry

Each is stated with its rask evidence and its seam. These are requirements, not
suggestions — several are scars this estate has already paid for once.

### B1. History and events carry identifiers and counts — never payloads

Activity inputs/outputs, Dapr history and published events carry **only** dataset URIs,
version numbers, fragment-metadata JSON, row-id ranges, counts and bounded error maps.
The estate has this leak twice and has already written down that it is a leak:
`services/flows/src/flows/models.py:178-186` (`NodeResult.payload_text`, uncapped, its own
docstring names the follow-up) and `open_dapr.md` §2.13 (`enumerate_chunks` returns the
whole key set, carried a second time as each child's input) — and history lands in the
single-replica CNPG Postgres that also holds the lineage graph (§5.5(4)). The shape to
copy is already in-tree: `ingest/workflow.py::ChunkResult` — fragments as metadata JSON,
errors as `key → reason`. **Seam:** the transform's result model + an AST gate in
`tests/unit/test_ingest_invariants.py` refusing payload-shaped activity returns.

### B2. Never pre-gate a Ray submission on capacity or resource labels

Custom resource labels are logical: an autoscaler can satisfy them on demand, and a worker
type advertising them may be scaled to zero at submission time. A pre-submission check
against a static cluster snapshot rejects work the scheduler would have queued and
satisfied. rask is on the right side today (`ray_submit.py` POSTs straight to
`/api/jobs/`) and will be tempted off it once bronze→silver starts queueing. Accept-time
gates may refuse on unit count, deadline and FGA — never on capacity. **Seam:** a
structural test over the submit path asserting `ray.nodes`/cluster-resource reads appear
nowhere.

### B3. The submission id carries the full work identity — finish the second axis

The first axis is DONE (`2d18976a`): `submission_id(stage, token, work=from→to)`.
The second axis is **code identity**: the id carries no image tag, so during a rolling
deploy a redelivered trigger landing on the new pod re-attaches to a job the old pod
submitted — old entrypoint, old `runtime_env` — and reports success. Fold the image tag /
chart `appVersion` the pod already carries into the id **as part of this build** (it
changes re-attach semantics across deploys, so it lands with the transform, not as a
hotfix). Also consider refusing a null token at this call site rather than defaulting.
**Must not touch:** `submit_or_reattach`'s delete-and-resubmit-after-terminal-failure
branch (the deterministic-id poison antidote) and `prune.py`'s terminal-only reaper rule.

### B4. Transform identity reaches the resume predicate

Resume is a property of the read (`ratch/core/driver.py:12-18`: `WHERE col IS NULL`,
checkpointed row ids, output-key diff) — and none of those re-derive a row when the
TRANSFORM changes. Fix an embedding deriver and `col IS NULL` never revisits rows the old
code filled. The estate half-knows this (the HTR path pins `RASK_HTR_MODEL_REVISION`) but
the pin never reaches the predicate. **Shape:** (a) a derived `Stage.identity` =
`(name, actor qualname, sha256(runner_env))`, and re-key `runner_env`'s `lru_cache` on the
pyproject digest, not the bare runner name (today an edit serves a stale env forever);
(b) write a `transform_version` (source sha or pinned model revision) **in the same commit
as the data** (the R26 lineage-column precedent), and make the resume filter
`col IS NULL OR transform_version <> :v`. Backfill restartability stays Lance-native
(`add_columns` batch_udf + `checkpoint_file`) — the orchestrator schedules it, never owns it.

### B5. Idle-worker reaping: measure first, then an opt-in `tasks_per_worker`

On earlier Ray versions the raylet's idle-reaping keeps ~`num_cpus_available` idle workers
regardless of soft limits, and workers reused back-to-back never enter the idle pool; the
only lever is `max_calls`, which **must be set on `ray.remote(...)` — it is silently
dropped if passed via `.options()`**. rask's `runner_ray_remote_args` and lance-ray's
`ray_remote_args` are both `.options()`-shaped channels, so a `max_calls` added there is a
silent no-op — refuse it there (raise, don't drop). **UNVERIFIED on our pin** (ray
2.56): re-measure before adopting. If adopted: an opt-in `tasks_per_worker` on
`ActorConfig`, default None, stateless task legs only, env-gated with the measurement
recorded (the `MAINTENANCE_*_ENABLED` pattern). **Must not touch:** the fixed
`ActorPoolStrategy` pools in `runners/htr` — cold-starting a TrOCR actor per task is the
regression those pools prevent.

### B6. Refuse new runs while draining — the flag exists, nothing reads it on admission

`app.state.shutting_down` is set in nine lifespans and read by exactly one thing
(`/readyz`). Nothing refuses a new run — and `services/ingest`, `services/compute`,
`services/flows` neither set the flag nor mount `service_kit.probes` at all (the workflow
host, the Ray client owner, and the graph runner). Sidecar-delivered work (cron POSTs,
pub/sub) arrives on the app port regardless of readiness, so a pass can start inside the
grace period. **Shape:** one shared `Depends(refuse_when_draining)` in service-kit,
applied in one commit to every run-creating and sidecar-delivered route; HTTP routes 503
RFC 9457, subscription routes return `{"status": "RETRY"}` — never DROP. Wire probes +
flags into ingest/compute/flows; ingest readiness = `workflow_runtime is not None`
(today a WorkflowRuntime start failure is swallowed with a warning and the pod takes
POSTs it can never execute). **Must not touch:** Dapr Workflow instances are resumed by
the runtime, not held by the pod — do not "drain" them; readiness never probes a
dependency.

### B7. Resolve once, carry the value — nothing downstream re-resolves

The parent workflow's **first activity** resolves the transform spec (one registry read,
no cache, no watch) and the resolved spec is stamped into the workflow input and carried
into every child, activity and Ray actor via `fn_constructor_kwargs` /
`runtime_env.env_vars`. No Ray actor reads `os.environ` or constructs `Settings()` to
learn what the run is doing. The estate reached this from the pain side, three times:
`ChunkSpec.dataset_uri` ("two derivations of one location is the bug"), `ChunkSpec.sizing`
(a rolling restart must not change a live run's fragment size), `flows::NodeJob` (a run
split across two clusters).

### B8. A declared `TransformSpec` record, vocabulary-validated at admission

A `_transforms/` record kind under the catalog control root, peer of `_policies/`,
reusing `maintenance_policies`' `_key`/`list`/`get`/`put`/`delete` shape. Frozen pydantic,
`extra="forbid"` (flows already recorded what `extra="ignore"` cost: any wrong-shaped body
parsed as a valid empty graph), no `options` bag:

```
source_namespace   target_namespace   target_table
lane               # enum over server-declared lanes; unknown → 422 naming the key, never a 200
actor: ActorConfig # + resources: dict[str, float]  (explicit field — a per-stage worker-type
                   #   label is the lever a heterogeneous cluster needs; declaring it must
                   #   never mean pre-checking it, per B2)
batch_bytes        # NOT batch_rows — bronze rows are ~1.8 MB page images; memory bounds
                   #   are a PRODUCT of batch size × threads (the maintenance lesson)
enabled: bool
```

Write door: `POST /v1/namespace/{id}/transform/set`, `can_update_properties` (writer rung,
already on `namespace` — no FGA model change). Server does `exclude_unset` merge and
re-stamps identity after the merge (a wholesale union once silently cleared a policy
field). Ships **with** a reconcile category for orphaned specs and a project-scoped list
endpoint — a silently-deleted spec must never change behaviour with nothing to point at.
The record only *describes*; the catalog stays the only commit writer.

### B9. An oversized activity result becomes a handle — one threshold, one direction

```
result <= RASK_WF_INLINE_MAX_BYTES → rides Dapr history inline
result >  threshold                → written to staging_root(dataset_uri, run_id),
                                     returns {"$handle": key, "bytes": n}
```

Default **measured, not guessed** — measure `enumerate_chunks`' result at advertised scale
first (`open_dapr.md` open question #8; the 120 MB figure is the doc's own arithmetic,
unverified). Host it by generalising `ingest/staging.py` from "fragments awaiting commit"
to "per-run durable side-channel" — it already has the right properties (hash-of-the-work
naming so retries converge; exact-cover resolution; purge only after commit). No `get`
across runs, no `exists`, no fallback tier — one copy, so stale-read races are
unreachable by construction.

### B10. Monotonic clocks; the same number lands in the lineage facet

Every duration — coordinator activity, Ray stage, commit — uses `time.perf_counter`
(the rule is stated in exactly one place today: `flows/executor.py:236-240`), rides
`service_kit.setup_otel`, and the **same number** lands in the lineage run facet so the
graph and the metric cannot disagree. A CPU clock only for CPU-bound work, named as such.

### B11. Boot-env vs live-spec — two columns, written down

BOOT-ENV (S3 endpoint, Lance cache caps, OTLP endpoint, FGA store ids — worker-derivable,
restart to change) vs LIVE-SPEC (target table, batch size, actor sizing, enablement —
travels in the job payload per B7). Riders: secrets appear in **neither** column (Dapr
secret store only, fail-closed) — and note the live violation to fix with this build:
`ray_submit.py` puts `S3_SECRET` into `runtime_env.env_vars`, which the Jobs API echoes
back. Every bound is `int | None` with None meaning unbounded — never 0, never -1
(`older_than_days` carries the ge=1 scar note).

### B12. Randomise iteration order; count what a pass actually did

`sweep.py:179` iterates a deterministic listing — a pass that consistently dies at dataset
N never maintains anything after N, silently, forever (`open_dapr.md` §2.19 CONFIRMED) —
while the same module shuffles its retry list 140 lines later. And `record_run()` fires
only after the loop, so a process killed at item 400 of 900 is observationally identical
to a tick that never arrived (§2.20). **Shape:** shuffle or persist a rotation offset in
the sweep; a `started` counter before the loop + per-item counters inside it, emitted on
empty ticks too ("adding 0 CREATES the series"); the transform fan-out gets per-item
isolation and randomised order from day one. `open_dapr.md` §4 makes the counters a
**prerequisite** for any durability argument.

### B13. Reuse the lance-ray global Pool; single-flight claims get chart gates

When distributed compaction lands, `init_global_pool()` once in the maintenance lifespan,
`clear_global_pool(close=True)` on shutdown — the sweep is a 120 s cron over every
dataset; a pool per tick is the waste. The pool belongs to the process that owns the lane
(maintenance owns table health). Separately: four in-process single-flight sites claim
cluster-wide exclusivity via comments referencing `replicas: 1` chart values, and one
values bump turns two of them into lies with no test failing — add invariant-test
assertions binding each `asyncio.Lock`-as-cluster-lock to its replica count (or a real
distributed lock, like lineage's). Same commit: assert every Deployment probe path
resolves to a mounted route, and every chart-set `RASK_*` env has a reader (both have
shipped broken before).

### B14. One `transform_batch`, two drivers, one drift pin

One `transform_batch(source_uri, target_uri, spec) -> Result` driven by BOTH the Ray job
entrypoint and the local test, pinned by a drift test beside `test_ray_stage_job.py` (the
estate's own answer to two-paths-one-contract). Its docstring states what the local lane
cannot certify: fan-out concurrency, `resources` satisfaction, GPU packing, lance-ray's
blob-typing (CLAUDE.md: verify like it ships). Storage against moto or a real `.lance`
dir, never a mocked sink; selection/ordering logic fuzzed against a brute-force oracle
(the `_exact_cover` precedent — it raised on 24% of inputs that had a perfect cover); no
behaviour claim discharged by a signature check.

### B15. Bound dashboard reads; a cache has one owner and a cron, not a thread

`ray_kit/dashboard.py` bounds at the source (MAX_JOBS/MAX_TASKS, with the 81,155-job OOM
measurement attached) but `compute/routes.py` still makes live dashboard round-trips per
request. First lever stays source-bounding. If a cache is added: compute owns it, written
from a **Dapr cron binding**, never an in-process refresh thread (the A13 gate is
repo-wide against in-process polling). `/ray/health` stays live but bounded.

---

## 3. The Ray checklist — tick before the transform ships

1. **Drivers stay ephemeral** — every Ray driver exits with its job; a `ray.remote` in a
   long-lived process leaves a permanent GCS function-table entry. Forbid
   `ray.init`/`ray.remote` in `services/*` (structural test).
2. If a long-lived driver is ever adopted: cache only the base `ray.remote(cls)` keyed on
   `Stage.identity` (B4); apply `.options()` per submission — a cached post-`.options()`
   object freezes one submission's resources onto every later one.
3. `max_calls` via an `.options()`-shaped channel is a silent no-op — refuse it (B5).
4. Idle-worker reaping: re-measure on ray 2.56 before believing anything (B5).
5. Never pre-gate on labels (B2).
6. **No `ray://` client in a service, ever** — services speak Jobs REST with explicit
   timeouts; a dropped Ray Client connection can freeze the calling process. Keep it that
   way.
7. Blocking driver-side calls (`lance_ray.read_lance`/`write_lance`/`compact_files`) go
   through a thread from FastAPI — better, submit a Ray **job** and keep `ray` out of the
   service image entirely (`ray_submit.py`: "no ray package in the mover image").
8. No fixed-name detached singletons — a deploy breaks the one-owner assumption. rask's
   equivalent singleton is the submission id: B3 is the fix.
9. Reaping deletes only terminal jobs (`prune.py` already: "deleting live work is not
   retention, it is sabotage"). No change.
10. `runtime_env` stays env-vars-only; code arrives via the Dagger-built image. Nobody
    "fixes" a missing dependency by uploading it at submit time.
11. Collection happens in the driver ("actors compute, the driver commits") — never
    `ray.get` inside a nested remote task. The knowingly-paid exception is the Serve
    handle concurrency in `runners/htr`, documented as the throughput ceiling.
12. Heavy blobs never transit Ray Data blocks — blob stages ship `_rowid`s only
    (`driver.py:74-79`). This is why worker reaping cannot orphan a payload.
13. Ray knobs that must precede driver construction go at the top of the job entrypoint
    with a comment — never an import side effect in `packages/*`/`services/*`.
14. Health endpoints never talk to Ray (B15); a future compute `/readyz` that touches Ray
    needs `asyncio.timeout` + fail-closed — better, doesn't touch Ray.
15. Shutdown cancels nothing Ray-side — **a stated property**: the deadline leg of
    `when_any` produces a FAILED terminal outcome through the one terminal step; abandoned
    jobs are reclaimed by `prune_jobs` + the reconciler. (The losing `when_any` leg cannot
    be cancelled — `open_dapr.md` §3.)
16. Size reads in **bytes, not rows** — bronze rows are ~1.8 MB page images; memory is the
    product of batch size × threads (`MAINTENANCE_SCAN_BATCH_SIZE` × threads precedent).

---

## 4. Tier movement — the decisions (full analysis: open_ingest_design.md §4)

- **Readiness is the `published` tag, full stop.** The ack IS
  `POST /v1/table/{id}/publish` (exists, `can_update_tag`, runs the quality gate). No
  second readiness marker, no human-ack concept — readiness is the worker's statement.
- **Data moves when, and only when, a table's `published` tag advances** —
  `table_published` becomes the single cascade trigger (Option A), then
  `POST /v1/table/{id}/promote` on the **catalog** as the manual re-announcement door
  (Option B) — in that order, one design in two commits. A promotion door on the medallion
  producer is rejected (it would be weaker than the automatic path it duplicates).
- **The blocker:** the generic movers do not register what they write (measured: catalog
  `silver: []`, `gold: []` over real rows). `transform.py` must stop composing its target
  path and go through catalog create/register + commit + publish — the `htr_register.py`
  rewrite, generalised. A sequencing obligation, not just a diff.
- **Fix the project derivation** in `publication_trigger.py` (it reuses the catalog
  namespace as the project — every ingest-written table fails to cascade); better, the
  catalog puts `project` in the control event's `extra`.
- **Lane matching becomes a DECLARED SUBSCRIPTION on the source namespace**
  (namespace-scoped, `can_update_properties`, no model change). Both existing lane guards
  retire in one commit — deleting one moves the drop one hop later and changes nothing.
- **The quality gate sits at publish, in the catalog, and nowhere else.** The mover's
  local post-write gate is deleted once movers publish (its own docstring documents the
  hole: a mover-gated batch IS in the tier for anyone reading `latest`).
- **Manual doors:** manual push is bronze-only via merge_insert on (key, etag) —
  tuple-gated (`can_create_table` then `can_write_data`, create-on-parent). The manual
  bronze→silver mover (task #58) is the promote door + the transform, with the same
  lineage emission and governance as the automatic path. FGA needs **no new relations**
  (§4's table: `can_promote` on the target namespace + `can_get_metadata` on the source,
  two doors short-circuiting, the annotator's worked precedent).
- **Freshness:** scheduled poll is final (same-day is sufficient). The boundary that would
  justify a streaming layer is a *pushing* source or sub-minute freshness — neither
  exists.

Open questions carried from §4 (answer before or during the build): has
`/publication-arrival` ever fired end-to-end; does the control pubsub component actually
deliver `table_published` to the consumer (a head subscribed to a subject no stream
carries is indistinguishable from a filter that never matches); is
`medallion.compute: false` the intended production posture (if yes, silver/gold have no
writer and the design question changes); where does the lane subscription physically live.

---

## 5. The sketch

### 5.1 Workflow body

```
transform_run(ctx, req):
    spec        = yield ctx.call_activity(resolve_spec, req)          # B7: resolve ONCE, fresh
    partitions  = yield ctx.call_activity(plan_partitions, spec)      # returns POINTERS (B1/B9)
    for wave in waves(partitions):                                    # durable checkpoint per wave
        results = yield wf.when_all([ctx.call_child_workflow(partition_run, p) for p in wave])
    yield ctx.call_activity(finalize, ...)                            # ONE Append commit
    # every exit routes through emit_terminal — the ingest error-boundary shape
```

Inherited law from `ingest/workflow.py`: replay-safe clock only
(`ctx.current_utc_datetime`, `ctx.create_timer`); no httpx/clock/uuid in the body;
submission ids derive from `ctx.instance_id` + payload; one terminal step for every exit;
mid-run progress via `ctx.set_custom_status` — no side ledger.

`partition_run`: `submit_or_reattach` → durable wait (bounded `GET /api/jobs/{sub_id}`
status activity racing a deadline timer via `when_any`) → react.

### 5.2 The job

```
lance_ray.read_lance(source_uri, columns=[_rowid, ...])   # row ids only; blobs stay put
  -> map_batches(actor_cls, concurrency=(min,max), num_cpus/gpus,
                 resources=spec.actor.resources or None)
  -> driver collects fragment metadata (never payloads)
  -> catalog commit: read_version + run_id marker          # one writer
```

Resume: `WHERE col IS NULL OR transform_version <> :v` (B4).

### 5.3 Fan-out — three layers, each with one owner

```
Dapr Workflow → waves of partitions (durable, replayable, survives the pod)
Ray Data      → rows within one partition (streaming, resource-aware)
JetStream     → redelivery of a failed unit (retry, maxDeliver, DLQ)
```

Per-item failure is carried, not dropped: partition results are `ChunkResult`-shaped, and
`finalize` refuses to commit an empty fragment list. **A run that dropped 3 of 400
partitions must never report COMPLETE.**

### 5.4 Preconditions

1. Measure `enumerate_chunks`' result size at advertised scale (sets B9's default).
2. Land the B12 counters — prerequisite for any durability argument.
3. Re-measure ray 2.56 idle-worker behaviour before touching `max_calls` (B5).
4. ~~Fix `submission_id`~~ — **DONE**, first axis (`2d18976a`); the code-identity axis
   lands with this build (B3).
5. Verify like it ships: the kill test is `open_dapr.md` §5.7 Test B — kill the Ray head
   and confirm the workflow observes the job vanish rather than hanging until the timer.

---

## 6. Rejected — one line each, so nothing is re-litigated

- An in-process service supervisor / lifecycle controller — the k8s Deployment is the
  supervisor; one FastAPI app per service.
- A `POST /drain` endpoint — a process-local flag cannot mean "this deployment is
  draining" behind a multi-replica Service; the *admission* half is adopted (B6).
- An executor abstraction / swappable compute backend — rask chose its owners; a local
  lane that diverges from production certifies nothing (B14 is the honest version).
- A condition DSL over an event bus — events are refresh hints, never authoritative; the
  trigger surface is "a table's `published` tag advances" (§4).
- An object-store strategy enum (memory/artifact/fallback) — a second store with no
  version to order the copies; B9's one-threshold-one-direction replaces it.
- A detached fan-out actor — a fourth fan-out owner; the three layers in §5.3 each have
  one.
- `allow_partial_failure` dropping exceptions — the estate decided the opposite three
  times (errors are carried, keyed, counted).
- Arbitrary dotted-import-path actions — dispatch is a closed server-declared registry;
  a client-supplied import path is RCE with extra steps.
- Runtime plugin installation / entry-point discovery — extension seams are in-tree and
  test-gated; images are immutable.
- Client-side read-modify-write config apply — writes are server-side, FGA-gated,
  CAS-guarded.
- A pluggable document DB for control records — died at P7a; JSON records under the
  control root, CAS'd, schema-evolved by pydantic defaults.

---

## 7. Proposed goal

> **/goal — BATCH: bronze→silver transforms are declared, durable, and governed.**
> A `TransformSpec` declared on a source namespace drives a durable bronze→silver run:
> Dapr Workflow coordinates, one Ray job per partition computes, the catalog commits, the
> `published` tag gates the cascade. Conditions, each proven live with pasted output:
>
> 1. **DECLARED** — a `_transforms/` record written through the FGA-gated catalog door;
>    an unknown lane is a 422 naming the key; the record survives a pod restart.
> 2. **DURABLE** — a run killed mid-wave (pod delete) resumes from workflow history and
>    completes without re-running finished partitions; history contains pointers and
>    counts only (B1 gate green).
> 3. **COMPUTED ON RAY** — the partition job runs via `submit_or_reattach` with a
>    work+code-identity submission id (B3 complete); a redelivered trigger re-attaches;
>    a deploy does not.
> 4. **COMMITTED ONCE** — one Append per run through the catalog (read_version CAS +
>    run_id marker); a replayed run commits nothing twice; the movers' output is
>    REGISTERED (catalog lists silver non-empty).
> 5. **GATED** — the cascade fires only on `table_published`; a quality-gate failure
>    leaves data committed but unpublished and downstream unmoved; the manual promote
>    door refuses an unpublished source.
> 6. **RE-DERIVABLE** — bump the transform version, re-run, and only rows written by the
>    old version are revisited (B4's predicate, proven on real rows).
> 7. **GREEN** — pytest + ruff + the frontend gates, plus the new structural tests
>    (B1 payload gate, B2 no-capacity-gate, B13 replica-binding).
>
> Constraints carry over verbatim from the convergence goal: never write FGA tuples
> (report the exact missing tuple and STOP); never print credentials; images only via
> scripts/dagger-image.sh; deploy only images built from pushed main, tagged main-<sha>,
> never a SHA behind the running one; verify the code in the pod; read error bodies
> before diagnosing; a blocked step is BLOCKED with the reason, never quietly skipped.
