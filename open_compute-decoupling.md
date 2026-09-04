# open_compute-decoupling.md

**Working spec — is the estate coupled to Ray, and what does an executor contract look like.**
Scope: the compute engine only. The Lance format + namespace, OpenFGA, OpenLineage→AGE and the Dapr
building blocks are FIXED POINTS; coupling to them is correct and is not counted here.

---

## 1. The verdict

**The estate is not "coupled to Ray" evenly. It is coupled at exactly two structural places and leaks the
name in a dozen cosmetic ones — and the expensive problem is not the coupling, it is that the WORK
CONTRACT is unwritten.**

Three plain statements, each checkable:

1. **A non-Ray unit of work cannot be DECLARED.** `TransformSpec.entrypoint`
   (`packages/service-kit/src/service_kit/lakehouse/transform_specs.py:98`) is validated by
   `_baked_entrypoint` (`:129-149`) against `BAKED_JOBS_DIR = "/home/ray/jobs/"` (`:55`) and
   `BAKED_CLUSTER_JOBS = {"ray_stage_job.py","ray_train_job.py","ray_dummy_job.py"}` (`:69`). This is in the
   **shared platform library**, so the catalog inherits Ray through its own dependency and 422s a Spark
   declaration at `services/catalog/src/catalog/api/v1/endpoints/transforms.py:130`. The endpoint
   docstring ("will EXECUTE on the shared Ray cluster", "Ray documents `runtime_env` as
   development-only") is a FastAPI summary, so it is published into the OpenAPI and regenerated into
   `frontend/packages/api/src/generated/catalog.ts`.

2. **The estate already runs a SECOND executor and the declaration cannot express it.**
   `services/medallion/src/medallion/services/transform.py:677` — `use_ray = settings.ray_enabled` — is
   a boolean where an engine selector belongs; the `else:` branch calls `compute.transform_stage(...)`
   with no `spec`, no `entrypoint`, no `params`. `entrypoint`/`params`/`code_version` are read in exactly
   one place, `services/medallion/src/medallion/services/ray_submit.py:159-162`. So the governed record
   does not name the work; it names a Ray command line, silently ignored under the other engine. The
   compute zone asserts the opposite in a comment
   (`frontend/microfrontends/compute/src/lib/transforms.ts:4-9`, "A TRANSFORM IS NOT A RAY JOB") and the
   backend falsifies it.

3. **The output contract that makes a governed tier governed exists only as control flow in
   `scripts/ray_stage_job.py`.** Twelve obligations (governance columns, root provenance carry-forward,
   cardinality, parentless-row refusal, stable row ids, 2.2, delta semantics, merge-not-append, empty
   delta, empty source, blob round-trip, trace continuation). The platform enforces **three** of them
   post-write — `row_count_positive`, `not_null(key_column)`, `blob_resolves`
   (`packages/service-kit/src/service_kit/lakehouse/quality.py:84-99`, run decisively at
   `services/catalog/src/catalog/services/publication.py`). `TIER_COLUMNS`
   (`services/medallion/src/medallion/schemas/tier.py:82`) is imported by one file and it is a test.
   Nothing anywhere asserts that a published tier carries `stage`, `lineage` or a non-null
   `source_rowid`.

   This is not hypothetical. `runners/dummy` is a **declarable, baked, accepted** lane whose
   `SILVER_SCHEMA` (`runners/dummy/src/dummy_runner/transform.py:37-46`) has no `stage` column, no
   `lineage` column, and an **int64** `source_rowid` where `stage_stamp` mints uint64; it fabricates
   parents (`transform.py:63`, `list(range(len(ids)))` when `_rowid` is absent), re-mints from the
   immediate parent every run, never reads `LINEAGE_JSON`, and opens its destination with
   `lance.dataset(to_uri)` and no `storage_options` (`job.py:47-63`). Nothing is red. **A second engine
   is not a hypothetical risk to the contract; the second engine already violates it.**

And one finding the audits produced that nobody has acted on: **the platform sends the stage job seven
identity variables it does not read.** `ray_submit.py:233-256` forwards `FROM_ID`, `TO_ID`, `RUN_ID`,
`ORIGINATOR`, `PROJECT`, `LINEAGE_URL`, `LINEAGE_SERVICE_ID` with a comment explaining that the job emits
its own OpenLineage. `grep -n "os.environ" scripts/ray_stage_job.py` returns 11 lines and **none of those
seven is among them** — the stage job emits nothing; its provenance is the `lineage` column plus the
mover's pass-2 COMPLETE. The comment is true of `ray_dummy_job.py` and `ray_train_job.py` and false of the
default entrypoint. **The submitter cannot tell which obligations the entrypoint it names actually
fulfils, and neither can a reviewer.** That is the contract gap in one sentence.

### Seam table

| # | Seam | Ray in it | Verdict | Cost |
|---|---|---|---|---|
| 1 | **Work declaration** — `TransformSpec` + `POST /v1/project/{id}/transform/set` | `BAKED_JOBS_DIR`, `BAKED_CLUSTER_JOBS`, `entrypoint`, the published OpenAPI text | **engine-bound — BLOCKER.** A second engine cannot be declared at all | moderate |
| 2 | **Submission + watch** — `stage_run`/`train_run` → `ray_submit` → `ray_kit.submit` → Jobs REST | `_TERMINAL_OK/"SUCCEEDED"`, `_TERMINAL_BAD`, 404-means-unknown, `MAX_UNSEEN_POLLS`/`MAX_RESUBMITS`, `ray_*` fields on the bus payload, Ray's name in the FAIL facet | **engine-bound in contract, neutral in structure.** ~16 of 1563 lines in `workflow.py` are executable Ray | moderate |
| 3 | **Output proof obligations** — what makes a written tier acceptable | none by name; the contract IS one Ray script | **engine-IMPLICIT — the real debt.** 8–9 of 12 obligations enforced nowhere | **structural** |
| 4 | **Maintenance** (`services/maintenance` + the catalog's on-demand doors) | zero code; one docstring naming `lance_ray.compact_database`; `lance-ray` is nonetheless installed in the cluster image (`packages/ray-cluster-env/pyproject.toml`) | **decoupled from Ray, NOT engine-neutral.** Receipts, error taxonomy, cadence stamp, memory bounds and the #114 clearance all assume in-process. A second in-process implementation (the catalog doors) already diverges | moderate — **recommend defer** |
| 5 | **Ingest / acquisition** | zero — verified in deps, settings, chart, fixtures | **neutral for REPLACEMENT** (zero repo files change to swap the producer); the hand-off contract is prose, and the estate's own Ray writer honours 1 of its 5 verbs | moderate |
| 6 | **`services/compute` + `ray-kit`** | correct by design (a monitor is engine-shaped). Leaks: `ray_dashboard_url` on the SHARED `service_kit.config.Settings:50` (4 inheritors, 3 never read it); `/api/ray` + `/api/serve` in the public URL namespace; no test gating ray-kit's dependent set | **leaky** | low |
| 7 | **Dapr control plane** | cron plane clean (1 of 5 components engine-specific and correctly scoped to `compute`); **workflow HOSTING gated on `ray_enabled`** (`mover.py:91`, `producer.py:105`); `dapr-statestore.yaml:95` derives Component scopes from `.Values.medallion.ray`; `network-policy.yaml:248` selects on `ray.io/is-ray-node`; `gpu-coherence.yaml` keys on `ray.gpuCount` | **leaky** | moderate |
| 8 | **Identity model** | `_NOT_A_PERSON` enumerates `"ray"` (`services/catalog/src/catalog/core/lineage_emit.py:169`) — and there is a **drifted second copy** at `runners/dummy/src/dummy_runner/lineage.py:48` (adds `htr`, drops `anon`); `producer_author` defaults to `"ray"` (`config.py:443`) for a write Ray does not perform | **leaky, cheap, required** — a `spark`/`flink` author would be classified as a PERSON and get an inbox actor | low |

**Honest summary.** Making Ray one implementation of an executor contract is roughly **3–5 weeks** of
careful work, of which about **one week** (steps 1–4 below) is what actually unblocks a second engine.
The remaining time buys the thing that is worth more than a second engine: the tier contract stops being
a convention inside one script.

---

## 2. The executor contract

Five new modules, all in `packages/service-kit/src/service_kit/lakehouse/` — the same home, and the same
one-writer/one-reader object-store shape, as `transform_specs.py`, `gate_specs.py` and
`maintenance_policies.py`. `service-kit` must NOT gain a `ray` dependency; today it has none, and that
must remain true.

```
service_kit/lakehouse/
  work_order.py      # WorkOrder + its parts. WHAT must happen. No engine nouns.
  executor.py        # Executor Protocol, RunState, RunFailure, SubmitOutcome, Capability
  task_registry.py   # TaskRegistration records under <control_root>/_tasks/ — replaces BAKED_*
  attestation.py     # StageAttestation + verify_stage_output() — the platform re-derives the proof
  transform_specs.py # EDITED: entrypoint -> task
```

### 2.1 The declared record (edit, not rewrite)

`TransformSpec` keeps every field except one:

```python
class TransformSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str            # unchanged (already carries the lane->name AliasChoices)
    project: str         # unchanged
    from_id: str         # unchanged — a catalog identifier, already neutral
    to_id: str           # unchanged
    cardinality: str     # unchanged — stage_stamp.CARDINALITIES
    code_version: str    # unchanged — which build this is declared against
    params: dict[str, str]  # UNCHANGED — already opaque, already RASK_PARAM_-namespaced

    # REPLACES `entrypoint`. A registered TASK KEY, opaque to the platform and to the catalog.
    task: str = Field(
        validation_alias=AliasChoices("task", "entrypoint"),
        description="a task registered in <control_root>/_tasks/",
    )
```

`params` needs no replacement: it is already a `str→str` map the platform never reads. What is
Ray-specific is only its DELIVERY (`runtime_env.env_vars`), and delivery belongs to the adapter — Spark
sends `spark.executorEnv.*`, a container sends `env:`.

The `entrypoint` alias is a **migration mechanism, not politeness**, for the reason `transform_specs.py`
already records for the `lane`→`name` rename: the model is `extra="forbid"`, so an un-aliased rename
REFUSES an old record, and a refused declaration means a mover runs the chart's program while an operator
believes the record governs it.

### 2.2 The task registry — keep declaration-time refusal, drop the Ray literal

Today the allowlist is a compile-time constant in a shared library, pinned to a dockerfile by
`tests/unit/test_ray_job_images.py:180-199`. That is why the catalog knows what Ray is.

Make it a runtime registry the **executor plane** writes and the **catalog** merely consults —
`<control_root>/_tasks/<task>.json`:

```python
class TaskRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str                       # "stage-transform", "train", "dummy-silver"
    engine: str                     # "ray" | "inprocess" | "spark" | ...
    command: str                    # the ENGINE'S business: "python /home/ray/jobs/ray_stage_job.py"
    code_version: str = ""          # the build this registration describes
    cardinalities: list[str] = []   # which CARDINALITIES this task can honour ([] = all)
    obligations: list[str] = []     # which of O1..O12 this task claims to satisfy (§2.5)
```

`transform/set` then: resolve `body.task` in `_tasks/`; absent → **the same 422 it raises today**
(`transforms.py:87-103` already builds exactly that error for an undeclared name). The refusal survives
intact and gets *stricter* — it can now also check "registered for an engine this estate runs" and
"supports the declared cardinality", neither of which a substring check can do. The catalog then contains
no engine name, no path, no filename.

**Rejected alternative:** serving the registry from `compute` (`:8804`). It gives the catalog an outbound
dependency on the submit plane and a fail-closed policy question on every declaration. The object-store
form has neither and matches three existing precedents.

**Who writes it:** the image build (`.docker/ray-cluster.dockerfile`'s COPY list is the source of the
`engine: "ray"` rows), or a boot-time registration by the executor plane. See open question 4.

### 2.3 `WorkOrder` — WHAT must happen

Lift `ray_submit.py:175-256` verbatim. That dict is already the executor contract, engine-free; only its
transport and the program's name are Ray-shaped.

```python
# service_kit/lakehouse/work_order.py

class WorkSource(BaseModel):
    uri: str
    table_id: str                     # catalog identifier, e.g. "acme-bronze$events"
    version_floor: int | None = None  # None => full scan; int => _row_created_at_version > floor

class WorkDestination(BaseModel):
    uri: str
    table_id: str
    merge_key: str = "id"
    write_mode: Literal["merge_insert", "overwrite"] = "merge_insert"

class WorkStamp(BaseModel):
    stage: str                        # the governed tier: bronze | silver | gold
    cardinality: str                  # stage_stamp.CARDINALITIES — DECLARED, never inferred
    lineage_document: str = ""        # R26 JSON; "" means DROP any inherited one

class WorkIdentity(BaseModel):
    run_id: str
    project: str = ""
    originator: str = ""              # a PERSON or "" — never a role, never an engine name
    code_version: str = ""

class WorkObservability(BaseModel):
    traceparent: str = ""
    tracestate: str = ""
    otlp: dict[str, str] = {}         # OTEL_* — standard, not Ray-standard
    service_name: str = ""

class WorkOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str                         # resolved from the TransformSpec, validated against _tasks/
    source: WorkSource
    destination: WorkDestination
    stamp: WorkStamp
    identity: WorkIdentity
    observability: WorkObservability = WorkObservability()
    params: dict[str, str] = {}       # opaque; the adapter applies RASK_PARAM_
    credential_ref: str = ""          # a NAME the executor resolves. NEVER a credential value.
    idempotency_key: str              # deterministic in (stage, token, from->to, code_version)

    def to_env(self) -> dict[str, str]: ...   # the ONE serialization; adapters may not hand-roll it
```

Two rules the shape enforces rather than documents:

- **`credential_ref` names, never carries.** `ray_submit.py:186-201` already refuses to put `S3_SECRET`
  or `S3_KEY` in the body because the Jobs API echoes `runtime_env` on an unauthenticated dashboard, and
  the last three commits (`f0261d8e`, `2d5688aa`) exist to put the Ray plane on a *scoped* credential the
  control plane cannot reach. A `WorkOrder` carrying `storage_options` would undo that by signature.
  Pinned estate-wide already by `tests/unit/test_no_credential_rides_the_submission.py` and
  `tests/unit/test_ray_submissions_carry_no_secret_estatewide.py` — re-point both at `WorkOrder.to_env()`.
- **`to_env()` is the only serialization.** Ray's `runtime_env.env_vars` merge-over-process-env semantics
  are the *adapter's* knowledge and stay in the adapter.

### 2.4 The `Executor` port

```python
# service_kit/lakehouse/executor.py

class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"     # the engine has no record of this handle — see below

TERMINAL: Final = frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED})

class Capability(StrEnum):
    DURABLE_RECORD = "durable_record"   # a submitted run survives a control-plane restart
    CANCEL = "cancel"
    FAILURE_DETAIL = "failure_detail"

class RunFailure(BaseModel):
    kind: str                # adapter-classified: "driver_error" | "oom" | "infra" | "unknown"
    message: str
    exit_code: int | None = None   # POSIX; 137 means SIGKILL under any engine

class SubmitOutcome(StrEnum):
    SUBMITTED = "submitted"
    REATTACHED = "reattached"
    RESUBMITTED = "resubmitted"      # a terminally-failed prior run was replaced

class RunHandle(BaseModel):
    engine: str
    handle: str                      # returned by submit, NEVER re-derived by the watcher

class Executor(Protocol):
    name: str
    capabilities: frozenset[Capability]

    def validate_task(self, reg: TaskRegistration) -> None: ...
    async def submit(self, order: WorkOrder, reg: TaskRegistration) -> tuple[RunHandle, SubmitOutcome]: ...
    async def status(self, handle: RunHandle) -> RunState: ...
    async def failure(self, handle: RunHandle) -> RunFailure | None: ...
    async def cancel(self, handle: RunHandle) -> None: ...
```

**`UNKNOWN` replaces today's overloaded `None`.** `ray_kit.submit.job_status` returns `None` for a 404 and
`workflow.py:313-324` disentangles three distinct meanings from it by hand — not-yet-registered,
record-lost, transport blip — using `seen` / `vanished` / `never_registered` / `MAX_UNSEEN_POLLS`.

**The resubmit machinery becomes capability-gated, not unconditional.** `workflow.py:104-121` justifies
`MAX_UNSEEN_POLLS=4` and `MAX_RESUBMITS=2` by an engine-specific durability defect it names outright:
"Ray's GCS is not fault-tolerant here (no external Redis, a deliberate estate-wide `no Redis`), so a head
restart takes every job record with it." Against an executor advertising `DURABLE_RECORD`, that machinery
is a spurious double-submit. Rule: **an executor WITHOUT `DURABLE_RECORD` may answer `UNKNOWN` after a
real status, and only then may the workflow resubmit.**

### 2.5 Proof obligations — O1…O12

These are what `scripts/ray_stage_job.py` enforces on itself today and what nothing enforces on anyone
else. They belong in `service_kit/lakehouse/attestation.py`, as `verify_stage_output(order, dataset) ->
list[Assertion]`, **re-derived by the platform from the written dataset**, never believed from a
self-report. That is the difference between a contract and a convention.

| id | Obligation | Today enforced where | Today enforced for a second engine |
|---|---|---|---|
| **O1** | Output carries `id`, `stage`, `source_rowid`; and `lineage` when a document was supplied | `stage_stamp.stamp_stage`, called by both drivers | **no** |
| **O2** | `source_rowid` is **uint64** | `stage_stamp.py:81-82` | **no** (dummy writes int64) |
| **O3** | Output schema equals `stamp_stage(upstream.schema.empty_table()).schema` extended by the transform's own columns — an equality against a reference function, so two constructions cannot agree by luck | `ray_stage_job.py:149` | **no** |
| **O4** | `count_rows("source_rowid IS NULL") == 0` — provenance for every row, all cardinalities, delta and full alike | `ray_stage_job.py:571`, `_assert_stage_contract` | **no** |
| **O5** | Root provenance CARRIES: an input already holding `source_rowid` keeps it; only a head mints from `_rowid` | `stage_stamp.carry_source_rowid` | **no** (dummy re-mints — silently reroots the chain one tier down) |
| **O6** | Cardinality honoured: on `1:1`, `rows_out == rows_in` over the rows this run read. Unknown cardinality is REFUSED, never defaulted | `ray_stage_job.py:402-426` | **no** |
| **O7** | `data_storage_version == "2.2"` and `has_stable_row_ids` | `ray_stage_job.py:236-252,567` | **no** (`quality.py` checks neither) |
| **O8** | A blob-typed input column is blob-typed in the output. **Separate from `blob_resolves`**, which iterates `blob_field_names(ds.schema)` and therefore emits ZERO assertions on a demoted column | nothing | **no** |
| **O9** | Idempotent: redelivery of the same `run_id` does not grow the row count (merge_insert, never append) | `ray_stage_job.py:462`, `dummy_runner.write_silver` | partially |
| **O10** | An empty delta writes NOTHING — no new version. "Writing an empty version would fire a publication event for data nobody added" | `ray_stage_job.py:499-504` | yes (dummy) |
| **O11** | An empty SOURCE still produces the destination | `ray_stage_job.py:246-252` | **no** |
| **O12** | Trace continuation: run under a span parented on the supplied context; **absent context is untraced, never fabricated**. And `lineage.dataset_id` is stamped on the written schema | traces: both; `lineage.dataset_id`: **in-process only** (`compute.py:192,204`), read by `maintenance/core/lineage_emit.py:149` — Ray-written tiers silently lose the sweep's per-dataset FAIL surface | **no** |

Plus the two governance obligations that are ALREADY correct and must be defended, not moved:

- **W1 — write authorization is the PLATFORM's, before dispatch.** `transform.py:869-886`
  `authorize_stage_write` runs the `can_write_data` rung at the catalog's own door BEFORE `_write_stage`
  dispatches, precisely because "the Ray job opens the destination with the pod's ROOT credential and
  authorizes nothing". This is the model for the whole seam: the engine never authorizes; the platform
  authorizes and records the decision.
- **W2 — the acceptance door is the catalog's `publish`.** `publication.py` runs the gate and refuses a
  backwards tag move. `verify_stage_output` must land **there** (see open question 7).

### 2.6 Submission and watching

`stage_run` keeps its entire shape — durable `ctx.create_timer`, one poll per turn, `continue_as_new`,
bounded history, the `publish_stage_ready` error boundary, the `succeeded|failed|abandoned|unnotified`
verdict vocabulary (which is the WORKFLOW's judgement and explicitly not the engine's status) — and loses
only the Ray nouns:

```
submit(order)                -> RunHandle          # activity: submit_work
status(handle)               -> RunState           # activity: poll_work
RunState in TERMINAL         -> publish / report
```

Wire renames on `StageTrigger` (`trigger_guards.py:138,164,165`) — **a migration, not an edit**, since
messages are in flight:

| today | neutral |
|---|---|
| `ray_job_done: bool` | `compute_done: bool` |
| `ray_submission_id: str \| None` | `compute_run_handle: str \| None` |
| `ray_duration_seconds: float \| None` | `compute_duration_seconds: float \| None` |

`extra="ignore"` (`trigger_guards.py:102`) makes ADDING the neutral keys safe immediately; REMOVING the
`ray_*` ones is not, so both are read for one release and only the neutral ones are written.

And the FAIL facet: `workflow.py:623` `f"the Ray stage job {outcome.submission_id} ended ..."` becomes
`f"the {handle.engine} stage job {handle.handle} ended ..."`. The AGE graph is a fixed point and durable;
it should not carry a hard-coded engine name in prose forever.

**Proof obligations the SEAM must satisfy, engine-free** (each corresponds to a defect already recorded in
these files, so each is a regression test, not a hypothetical):

- **P1 Idempotency.** Two submits with the same `idempotency_key` yield one run. (`ray_submit.py:169-174`
  records the collapse bug: a token-less trigger collapsed every submission onto `ray-<stage>-notoken` and
  `submit_or_reattach` read the collision as success — the second transform silently never ran.)
- **P2 The handle is RETURNED, never re-derived.** (`ray_submit.py:134-139`: `code` was added at the
  submitter and not the watcher, so every healthy job reported `abandoned`.)
- **P3 Build isolation.** The same work under a different `code_version` is a DIFFERENT run.
  (`config.py` `ray_code_version`: a rolling deploy re-attached to the old build and reported the new
  build's provenance over the old build's output — "worse than a failure because nothing is red".)
- **P4 The destination is not measured before terminal-OK.** The entire reason `workflow.py` exists.
- **P5 Terminality is observable, or the watch declares `abandoned` — never `failed`.**
- **P6 Loss is DECLARED, not inferred** (`Capability.DURABLE_RECORD`, §2.4).
- **P7 No credential rides the submission**, whatever the engine's echo semantics.
- **P8 Terminal-state mapping is TOTAL:** every state an engine can report maps to exactly one `RunState`.

---

## 3. What Ray becomes

Ray becomes **one adapter and one task-registry row.** Almost nothing is rewritten; the existing code
moves behind the port.

```
services/medallion/src/medallion/executors/
  __init__.py     # the registry: {"ray": RayExecutor(), "inprocess": InProcessExecutor()}
  ray.py          # <- services/medallion/services/ray_submit.py, unchanged in behaviour
  inprocess.py    # <- wraps compute.transform_stage; the second implementation, at last behind the seam
```

- **`packages/ray-kit` stays Ray-specific and stays where it is.** It is the adapter's library, not the
  contract. `submission_id` (`submit.py:55-87`) already computes an engine-neutral idempotency key and
  only its ID FORMAT is Ray-constrained (charset, 200 chars); `trace_env` (`:114`) is W3C, not Ray.
  `TERMINAL_OK`/`TERMINAL_BAD` (`:47-48`) become the adapter's mapping table, not a vocabulary the
  workflow imports:

  | Ray | `RunState` |
  |---|---|
  | `PENDING` | `PENDING` |
  | `RUNNING` | `RUNNING` |
  | `SUCCEEDED` | `SUCCEEDED` |
  | `FAILED` | `FAILED` |
  | `STOPPED` | `CANCELLED` |
  | 404 / no record | `UNKNOWN` |

- **`RayExecutor.capabilities` omits `DURABLE_RECORD`**, which is what re-enables `MAX_UNSEEN_POLLS` and
  `MAX_RESUBMITS` — for Ray, honestly, as a per-adapter policy rather than as module constants in a
  generic workflow.
- **`.docker/ray-cluster.dockerfile:139`'s COPY list becomes the source of the `engine: "ray"` rows** in
  `_tasks/`. `tests/unit/test_ray_job_images.py:180-199` stops pinning a library constant to a dockerfile
  and starts pinning the REGISTRATION to it — same guarantee, one plane down, and `service_kit` no longer
  holds a Ray container's directory layout.
- **`services/compute` stays exactly as it is.** 499 lines, eight one-line delegations, a `GET`/`HEAD`-only
  proxy, and a Dapr-cron job pruner that exists to work around a Ray defect (81,155 jobs / 164.7 MB in one
  listing, measured). A monitor for Ray *should* be Ray-shaped.
- **`MEDALLION_RAY_ENABLED` → `MEDALLION_ENGINE`** (`"inprocess" | "ray" | ...`), and
  `transform.py:677`'s `use_ray = settings.ray_enabled` becomes an adapter lookup. **This also unbinds the
  workflow runtime from the engine**: `mover.py:91` and `producer.py:105` gate a *generic durable
  capability* on the engine flag today; copy `services/flows/src/flows/lifespan.py:93`'s gate — does this
  pod have a sidecar and does this app host a workflow — which is the correct question and already exists
  in the estate. `chart/templates/dapr-statestore.yaml:95-99` then derives its `scopes` from the **mover
  list**, not from `.Values.medallion.ray`. (Render-verified: `--set medallion.ray=false` today drops all
  four medallion app-ids from the actor state store's scopes, and daprd then logs "Workflow engine
  started" and nil-derefs on the first dispatch.)

---

## 4. The migration

Ordered. Each step lands alone, is independently valuable, and has a test that proves it.
**[R]** = required for a second engine. **[T]** = tidier / correctness only.

---

**Step 0 [T] — gate `ray-kit`'s dependent set.** One invariant test asserting `ray-kit` appears in exactly
`services/compute/pyproject.toml` and `services/medallion/pyproject.toml`. The set is correct today and
nothing enforces it; one `service-kit` dependency line would make Ray estate-wide with nothing red.
*Test:* `tests/unit/test_invariants.py::test_only_the_submit_and_introspection_planes_depend_on_ray_kit`.
**~1 hour.**

**Step 1 [R] — `WorkOrder`.** Lift `ray_submit.py:175-256` into
`service_kit/lakehouse/work_order.py`; `RayExecutor` builds its `runtime_env.env_vars` from
`WorkOrder.to_env()`. No behaviour change.
*Test:* the posted env dict is byte-identical to today's for a fixed order (golden test); re-point
`test_no_credential_rides_the_submission.py` and `test_ray_submissions_carry_no_secret_estatewide.py` at
`to_env()`. **~1 day.**

**Step 2 [R] — `RunState` + the `Executor` protocol; Ray implements it.** `workflow.py` imports
`RunState`/`TERMINAL` and drops `_TERMINAL_OK`/`_TERMINAL_BAD`.
*Test:* `test_terminality_agrees_with_ray_kits_own_constants` becomes
`test_the_ray_adapter_maps_every_documented_ray_state` (**totality**, P8) — plus a test that
`services/medallion/src/medallion/workflow.py` contains no string matching `(?i)ray`. **~2 days.**

**Step 3 [R] — the task registry replaces `BAKED_*`; `TransformSpec.entrypoint` → `task`.** The blocker.
Includes the `AliasChoices("task","entrypoint")` compat, the catalog docstring rewrite (it is published
OpenAPI), and the regenerated `frontend/packages/api/src/generated/catalog.ts`.
*Tests:* (a) `test_the_task_registry_matches_what_the_cluster_image_bakes` — the pin moves from
`BAKED_CLUSTER_JOBS` to the `engine: "ray"` registrations; (b) **RED-first**: declaring
`task="spark-stage"` against a registered spark row succeeds, and an unregistered task 422s naming the
key; (c) grep test — `services/catalog/src/` and
`packages/service-kit/src/service_kit/lakehouse/transform_specs.py` contain no `/home/ray`, no
`ray_*_job.py`, no "Ray" in a field description. **~3 days.**

**Step 4 [R] — engine dispatch on the mover, and the in-process lane becomes a real adapter.**
`settings.engine` replaces `settings.ray_enabled`; `mover.py`/`producer.py` gate the workflow runtime on
the sidecar; `dapr-statestore.yaml` scopes derive from the mover list. **The in-process path stops being a
bypass**: it registers as a task and runs through `submit → poll → publish` like any other, which is what
finally exercises the seam with two implementations.
*Tests:* (a) the in-process lane's stage runs through `stage_run` end-to-end; (b) **the two adapters
produce byte-identical governance columns and identical column ORDER** for one fixture — the drift
`stage_stamp` was written to close, now asserted across adapters rather than within one function;
(c) render diff: `--set medallion.engine=inprocess` no longer empties the actor state store's scopes.
**~4 days.** *(See open question 5 — this step has a real cost.)*

**Step 5 [R] — the neutral bus contract.** Add `compute_done` / `compute_run_handle` /
`compute_duration_seconds`; write the neutral keys, read both; the FAIL facet names `handle.engine`.
Delete the `ray_*` fields one release later.
*Test:* a trigger carrying only `ray_*` still drives the measure path; one carrying only the neutral keys
does too; both are covered in `services/medallion/tests/test_stage_workflow.py`. **~1 day + a release
window.**

**Step 6 [R] — the identity model stops enumerating engines.** `_NOT_A_PERSON` is a denylist of names
where a TYPE belongs: a `spark`/`flink`/`dask` author following the estate's own reference pattern is
classified as a PERSON and `services/notifications/.../lineage_events.py:122-134` puts it straight into an
`InboxActor`. There are **two copies and they have already drifted** (`lineage_emit.py:169` has `anon`,
lacks `htr`; `runners/dummy/.../lineage.py:48` has `htr`, lacks `anon`) — and `reconcile`, stamped by
`services/lineage/.../repository.py:837`, is in neither. Measured live: `data_eng` 246, **`ray` 187**,
`analyst` 124, `reconcile` 49 (`tests/unit/test_cascade_originator.py:4`).
Fix: one definition in `service-kit`, and an `author.kind` discriminator (`person | service | engine`) so
the check is a type test, not an enumeration. Same step: `producer_author` default `"ray"`
(`config.py:443`, `chart/values.yaml`) is a **false record** — that write is performed by
`compute.py`'s in-process path, not by Ray — and becomes the service identity.
*Test:* `test_an_unknown_engine_name_is_not_addressable_as_a_person`; a single-definition invariant.
**~1 day.**

**Step 7 [R for correctness, and the highest-value step in this document] — `verify_stage_output`.**
Implement O1–O12 in `service_kit/lakehouse/attestation.py`, re-derived from the written dataset, and run
it at the catalog's publish door beside `quality.assert_quality` (see open question 7).
*Test:* this goes **RED on the estate as it stands** — `runners/dummy`'s silver output fails O1 (no
`stage`, no `lineage`), O2 (int64), O5 (re-minted parents) and O12. That RED is the proof the step is
worth taking; fixing the dummy runner is part of the step. **~5 days**, and it is where the structural
cost lives.

**Step 8 [T] — `ray_dashboard_url` off the shared base.** Move `service_kit/config.py:50` onto
`ComputeSettings`. Three of its four inheritors (`annotator`, `viewer`, `search`) never read it.
*Test:* a grep invariant — no engine URL on `service_kit.config.Settings`. **~2 hours.**

**Step 9 [T, owner call] — the public URL namespace.** `/api/ray` + `/api/serve` →
`/api/compute/{engine}/…`, and `chart/values.yaml` grows an `engines: [{name, kind, introspection_url,
auth_secret}]` list instead of a singleton `ray.dashboardUrl`. Touches the gateway,
`frontend/packages/api/src/ray.ts` (~15 schemas, 10 fetchers), two zones' remote functions,
`chart/alerting/rules.yml:443` and `tests/unit/test_ray_job_wire_parity.py`. **Cosmetic for a second
engine's correctness; not cosmetic for anyone with a bookmark.** ~3 days. See open question 2.

**Step 10 [T] — chart leaks outside the medallion.** `network-policy.yaml:248-254` allows S3 ingress by
`ray.io/is-ray-node` label (another engine's pods get **silent** object-storage denial — "green pods,
empty corpus"); `gpu-coherence.yaml:5,22,30` + `runtimeclass.yaml:6` make `ray.gpuCount` the chart's ONE
GPU signal with two render-time `fail`s on it; `explorer.yaml:227-232` and `frontends.yaml:166` derive
Serve origins from `ray.dashboardUrl`. Each is a small edit; together they are the difference between a
second engine that runs and one that looks healthy and reads nothing. **~2 days.**

**Not on the list, deliberately: maintenance.** See §5.

---

## 5. What we deliberately do NOT decouple

**The fixed points.** Lance file format + Lance Namespace; OpenFGA and the `can_*` relations over
`project > warehouse > namespace > table`; OpenLineage into AGE; the Dapr building blocks (actors,
secrets/OpenBao, pub/sub on NATS JetStream, state, bindings). Every one of these appears throughout the
contract above **on purpose**: `WorkOrder.source.uri` is a Lance URI, `WorkStamp.lineage_document` is an
OpenLineage-shaped document, W1 is an FGA rung, and the watcher is a Dapr Workflow. A `WorkOrder` that
abstracted over table formats or authorization models would be a second architecture, not a decoupling.

**Dapr Workflow stays the watcher.** ~1547 of `workflow.py`'s 1563 lines are engine-free and correct: the
deterministic clock, one-poll-per-turn + `continue_as_new`, the bounded-history reasoning, the error
boundary that turns a lost wake-up into `unnotified` rather than a fabricated failure, and the whole of
`promotion_review` (zero Ray). Do not replace this with an engine-side watcher. **Do not** let a second
engine bring its own orchestrator.

**`services/compute` and `packages/ray-kit` stay Ray-shaped.** A cluster monitor is legitimately
engine-specific — Ray has actors and placement groups, Spark has executors and stages, and a
lowest-common-denominator "job" view shows less than either dashboard. The right structure is a
**registry of engine-shaped adapters**, not one interface. A second engine gets a sibling service; neither
pretends to be the other. Verified: nothing in the lakehouse plane imports or HTTP-calls `compute` —
`RASK_COMPUTE_URL` appears in exactly one file, `services/gateway/src/gateway/config.py:77`. **If
`services/compute` vanished tomorrow the lakehouse would not notice**; only the operator UI and Ray's own
job-history retention would.

**Ray's name in the OpenLineage `job.namespace` / integration facet is CORRECT.**
`services/lineage/src/lineage/seed.py:46-47` (`_JOB_NS = "ray-jobs"`, `_INTEGRATION = "RAY"`) is demo seed
data; in production a Ray driver naming its own engine is exactly what that field is for. The only fix
needed is that it come **from the executor that ran the work**, not from a module constant.

**`runtime_env.env_vars` merge-over-process-env credential scoping stays in the Ray adapter.**
`ray_submit.py:198-201` depends on Ray merging the submission's env OVER the pod's — "measured twice on
the live estate" — and that is precisely the kind of engine semantics an adapter exists to hold.

**The in-process lane stays.** It is not a placeholder; it is the estate's only proof that the seam can
have two implementations. Step 4 promotes it rather than deleting it.

**Ingest's three-doors rule and the catalog publish door stay.** `POST /produce`, `POST /ingest-media`,
`POST /train` are the whole ingest surface; adding a protocol-specific or engine-specific ingress would
make that protocol privileged. `publish` stays the single acceptance point — step 7 strengthens it rather
than adding a second door.

**Sealed runners stay sealed.** The contract stops at the tier columns and the `WorkOrder`. A runner's
stage graph, models, GPU packing and output shape remain its own business, and `params` remains opaque.

**MAINTENANCE: declare the verb, do NOT build a distributed path.** The contract there is
`maintain(uri, storage_options, plan, protected) -> DatasetResult`, and it is worth naming as a Protocol
beside `maintenance_policies.py` so the pylance implementation is the FIRST adapter rather than the
definition. But do not dispatch it anywhere, for four measured reasons:

1. `lance_ray.compact_files` **does not reduce fragments** at these pins — `tests/e2e-py/test_ray_batch_e2e.py:86-99` is a **strict xfail** recording 4→4 where native pylance does 4→2 on the identical shape — and `lance_ray.create_scalar_index` still raises (`.docker/ray-lance.dockerfile:23-25`). The distributed path is non-functional in both halves.
2. `lance_ray.compact_database` takes a whole database and decides per table itself, driving through all three of the estate's refusals: the manifest-feature gate, the #114 whole-estate protected-base pre-pass, and the F6(d) trash exclusion. Losing any is **measured data loss**, not untidiness.
3. The three ops are ONE ORDERED PASS (compact → optimize_indices → cleanup) for correctness, not performance; `lance_ray` can implement step 1 only, which would put the engine boundary in the middle of the pass.
4. The #114 clearance is a **snapshot** computed once at `sweep.py:536` and consumed through the loop. Dispatching per-dataset opens a submit-to-execute window in which a `shallow_clone` turns a permitted compaction into exactly the deletion #114 exists to prevent.

Before any of that, maintenance has a **nearer** problem that decoupling would not fix and would inherit:
it already has two in-process implementations and they diverge — the catalog's on-demand doors run
compact+optimize without reclamation, emit no lineage and never read `declared_table_id`, hardcode
`batch_size=64`/`num_threads=2` (numbers argued from the *maintenance* pod's 512Mi limit) in the *catalog*
pod, open the handle with **no shared Lance session**, and build `timedelta(0)` where
`MaintenanceSettings.older_than_days` carries `ge=1` to make that unconstructible. And the single-flight
guard is an `asyncio.Lock` whose tripwire (`tests/unit/test_invariants.py:4309`) reads only
`chart/templates/maintenance.yaml` — while the same destructive calls are reachable from the catalog
Deployment, which has an HPA. **Fix the second in-process adapter first. That is the honest maintenance
work item, and it is not a decoupling task.**

---

## 6. Open questions for the owner

1. **Do we want a second engine, or the OPTION of one?** They cost differently. Steps 1–6 (~2 weeks)
   remove every blocker and leave Ray as an adapter. Step 7 (~1 week) is the one that makes a second
   engine *safe* rather than merely *possible* — and it is the step that is worth doing even if a second
   engine never arrives, because it goes RED on the estate today.
2. **Do the public rows `/api/ray` + `/api/serve` move to `/api/compute/{engine}/…`?** The gateway comment
   at `__init__.py:192-195` argues these name the Ray cluster, not the service — which restates the
   problem. Moving them touches the generated TS client, two zones, an alert and a wire-parity test, and
   breaks external bookmarks. **I will not make this call.**
3. **How long is the `ray_*` → `compute_*` trigger compat window?** Messages are in flight in JetStream.
   One release? Two? Recommendation: one release reading both, and a metric counting `ray_*`-only arrivals
   so the drop is evidence-based.
4. **Who writes `_tasks/` — the image build, or executor-plane registration at boot?** Build-time keeps
   today's guarantee ("the declaration door knows what the image bakes") and keeps the registry
   deterministic; boot-time registration is more honest for a container/process executor with no image
   list. Build-time is my recommendation; boot-time is the one that scales to an engine we do not control.
5. **Does the in-process lane really go through Dapr Workflow (step 4)?** Doing so makes the seam
   genuinely two-implementation and closes the "submit → poll → publish has never been exercised by more
   than one engine" gap — but it adds a durable workflow to the default, currently-synchronous path, an
   actor state store dependency for an estate that may have opted out of Ray entirely, and latency to a
   lane whose whole appeal is that it has none. The alternative — an adapter that reports `SUCCEEDED`
   synchronously — keeps the seam typed but unexercised.
6. **Which is the truth: `MEDALLION_RAY_ENABLED` defaults `False` (`config.py:238`) or
   `chart/values.yaml:1124 ray: true`?** The frontend's comment asserts the code default is the deployed
   one and it is not. Whatever `MEDALLION_ENGINE` defaults to should be stated once and be true in both
   places.
7. **Where does `verify_stage_output` run — the mover, or the catalog's publish door?** The catalog is the
   stronger place: it is the acceptance point every writer already shares, and it is the only place a
   foreign engine's output cannot route around. It also means a per-publish scan (O4's null-count pushdown
   is cheap; O3's schema equality is free; O8's blob-type check is cheap; O6's `rows_in` needs the work
   order, which the catalog does not have). Recommendation: **O1, O2, O3, O4, O7, O8 at the catalog**
   (dataset-only, no work order needed); **O5, O6, O9, O10, O11, O12 at the mover**, with the split
   documented rather than discovered.
8. **`runners/dummy` — fix it, or keep it as a negative fixture?** It is the estate's e2e probe and it
   currently proves the contract is unenforced. Fixing it removes the demonstration; keeping it broken
   means a permanently non-conforming governed tier in every environment. Recommendation: fix the runner,
   and add an explicit non-conforming fixture in `tests/unit` that `verify_stage_output` is asserted to
   reject.
9. **The vending door.** `POST /v1/table/{id}/credentials?tier=write` exists, is called by
   `catalog_register.authorize_stage_write` — and its answer is **deliberately discarded**; the bytes move
   under the Ray pod's root credential. That is a documented, deliberate stop-short. A container or
   process executor has no pod credential to inherit, so it forces the question: does a second engine get
   **vended, table-scoped** credentials (finishing the door), or its own scoped secret per the
   `f0261d8e`/`2d5688aa` pattern? This is the one open item where the answer changes the byte path.
10. **Does maintenance get a Protocol now (~1 day, no dispatch) or nothing?** My recommendation is the
    Protocol plus the catalog-door convergence work, and explicitly **no** distributed maintenance path
    until `lance-ray` compaction and distributed index build are functional upstream.

---

## 7. Owner direction, 2026-08-31 — GitOps + CRs + Ray/Kueue

Recorded because it settles three questions this spec had left open, and it changes section 4's
ordering rather than sections 1-3's findings.

### 7.1 Keep Dapr Workflow. Add two CRDs.

The orchestrator question was posed as "swap Dapr Workflow for Airflow or Dagster". The answer is no,
and the reason is not loyalty to Dapr — it is that the question mixes two layers that GitOps keeps
apart:

**GitOps governs DESIRED STATE, not a running process instance.** Argo works the same way: a
`WorkflowTemplate` lives in git; a `Workflow` — a run — is created dynamically and is not in git.
So "Dapr Workflow is imperative code" is not a GitOps failure. Dapr Workflow sits at the layer of
Argo's CONTROLLER, not at the layer of Argo's templates, and the declarative half is a CRD either way.

Three orthogonal layers, and the estate can have all three:

| Layer | What it is | Where it belongs |
| --- | --- | --- |
| **Declaration** — what should exist | the lane / `TransformSpec` | a **`Transform` CR** in git, synced by ArgoCD. Replaces `POST /v1/project/{id}/transform/set` as the WRITE path; the catalog record becomes the reconciled projection. |
| **Run orchestration** — how a run proceeds durably | submit → poll → terminal → publish; approval waits | **Dapr Workflow**, unchanged |
| **Execution unit** — the compute | one job | a **`RayJob` CR**, which is what Kueue can admit |

Airflow and Dagster were rejected on evidence, not taste. Both bring a second scheduler and a second
database, both are configured in Python rather than in cluster objects — so both move the estate AWAY
from CRs — and neither holds what these workflows actually do: `promotion_review` is a 72-hour
`wait_for_external_event` racing `create_timer`, and `ingest_run` is a cancel event racing a
`max_run_hours` deadline. That is durable human-and-deadline state, the category those tools are
weakest at. Argo Workflows is the CR-native one and can express a suspend node, but it cannot hold a
durable timer that carries no JetStream ack, which is the reason the poll cannot live in the HTTP
handler in the first place.

### 7.2 Runs BECOME custom resources without adopting an orchestrator

This is the part the earlier draft missed. A `RayJob` **is** a CR. Submitting through it rather than
through Ray's Jobs REST API means:

- `kubectl get rayjobs` lists every run — runs are cluster objects, visible to GitOps tooling.
- **Kueue can finally see them.** `chart/templates/kueue-queues.yaml` already exists, gated on
  `kueue.enabled` — and every job today goes through the Jobs REST API, which Kueue cannot admit,
  queue or preempt. Kueue is wired and structurally bypassed.
- Gang scheduling and per-tenant quota become available, which is what a multi-node Ray job needs and
  what a shared cluster needs.

**This is the same seam as the executor adapter in §2.** A `RayJobExecutor` that creates a `RayJob` CR
and watches its status is one implementation of the `Executor` protocol; the Jobs-REST submitter is
another. So the CR move is not extra work beside the decoupling — it IS the first adapter, and it is
the one that pays for itself immediately.

### 7.3 Backfill — what is true, stated precisely

The earlier draft was imprecise and the owner was right to challenge it.

- **`catalog/services/cascade_backfill.py` is NOT data backfill.** It re-asserts FGA GRANTS over
  every registered warehouse. Different concern; do not cite it as reprocessing.
- **Incremental reprocessing EXISTS and lives in the lakehouse:** the delta lane, `BASE_VERSION` +
  `_delta_filter` in `scripts/ray_stage_job.py`, addressed by **Lance dataset VERSION**. Verified
  running live 2026-08-31 (`lane=delta` off `BASE_VERSION=106`). It is not tied to `lance_ray` —
  that import appears only in `scripts/ray_lance_job.py`, a separate demo — and it is engine-neutral
  in concept: any executor that can push a predicate can honour it.
- **What is missing is a RE-RUN VERB, not a mechanism.** The mover operator surface is list, inspect,
  terminate; there is no `POST /movers/{m}/stages/rerun`. So an operator cannot say "reprocess silver
  from version X" even though the machinery to do it exists.
- **Airflow-style backfill would not transfer.** Its backfill is a SCHEDULER concept — re-run a DAG
  over a DATE RANGE. This lakehouse is version-addressed, not date-partitioned, so adopting Airflow
  would supply a date axis the data does not have rather than the version-range re-drive it does need.

### 7.4 Revised order

1. ✅ **DONE 2026-09-04.** `WorkOrder`, `Executor`, `task_registry`, `attestation` — all in service-kit,
   which gains no `ray` dependency. `TransformSpec.entrypoint` became `task`, and the engine noun left
   the published contract: `grep "home/ray/jobs\|runtime_env" docs/catalog-openapi.json` went 6 -> 0.
   Dispatch reads the record too (`engine_choice`), so a declaration naming another engine is REFUSED
   rather than run on whatever is configured.
2. ✅ **DONE 2026-09-04.** `InProcessExecutor` conforms — `isinstance(x, Executor)` asked at runtime,
   and `_write_stage`'s non-Ray branch dispatches through it. What it proves is that a SYNCHRONOUS
   engine is expressible: `submit` returns a handle already terminal, `status` answers `UNKNOWN` for a
   handle this process never saw, `cancel` REFUSES because the capability is not advertised. A port
   that could not express that would be a Ray-shaped interface wearing a neutral name.
3. **`RayJobExecutor` — submit as a `RayJob` CR** rather than the Jobs REST API. First adapter, and
   the thing that makes Kueue functional.
4. **Kueue admission** — queues, quota, gang scheduling. Unblocked by (3), impossible before it.
5. **`Transform` CRD + reconciler** — the declaration moves to git; the catalog record becomes the
   projection.
6. ✅ **DONE 2026-09-04** — `POST /api/movers/stages/rerun`. It touches the Jobs API not at all: the
   fresh-token 409 was DROPPED, because the listing it needed has already OOM-killed this pod (81,155
   jobs / 164.7 MB in one response). So there is nothing here left to port when (3) lands. The
   reasoning is in `docs/DECISIONS.md`, "Cascade repair"; `open_cascade_repair.md` is deleted.

**"Steps 1-3 are required for a second engine" is FALSIFIED, and the correction matters for
sequencing.** A second engine landed at step 2 with no `RayJob` CR anywhere: the in-process executor
conforms to the port and `engine_choice` routes to it off the record. What step 3 is actually required
for is KUEUE (step 4) and BYO maintenance compute (M3) — admission and quota, not plurality.

**WHAT IS LEFT IS EXACTLY 3, 4 AND 5**, and they are one arc rather than three: submit as a `RayJob`
CR, admit it through Kueue, and move the declaration into git behind a `Transform` CRD. The last of
those has a precedent to respect — `docs/DECISIONS.md` (2026-08-16) rules the `Project` CRD abandoned
as a rask-repo concern because a CRD without its controller renders unreconciled CRs as objects stuck
mid-provision; a `Transform` CRD belongs to `rask-operator` for the same reason.

Steps 1, 2 and 6 are done and their specs are deleted. Nothing here needs Argo, Airflow, Dagster,
Temporal, or a second control plane.
