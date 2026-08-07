# open_flowdapt_analysis.md

**What this is.** A read of [emergentmethods/flowdapt](https://github.com/emergentmethods/flowdapt)
(Apache-2.0, v0.1.52 @ `6d83f04`, `ray[default]==2.54.1`) against rask, done to feed the
**bronze→silver batch transform + silver evolution design**, which is not yet designed.
Five areas were read at source: the Ray executor, the compute plane (stage/graph/parameterized
stage), the trigger service, the object store, the declarative resource plane, and the
service-lifecycle/plugin/eventbus substrate.

**Licence.** flowdapt is Apache-2.0, same as rask. Borrowing is licence-compatible. Where a code
*shape* is closely borrowed below it is marked **[borrowed: flowdapt, Apache-2.0]** and must carry
an origin comment in the rask file.

**Verification.** Every claim carries a file ref — a flowdapt path (upstream, repo-relative) or a
rask path (relative to this repo root). Claims that are inference rather than read code are marked
**UNVERIFIED**.

---

## 1. What flowdapt IS — and why rask is not that

flowdapt is an **executor-agnostic workflow engine for live ML**: you declare a workflow as a list
of *stages* (`target` = a Python callable or an import string, plus `depends_on`), post it as a
k8s-shaped `kind/metadata/spec` document, and an `Executor` — Ray, Dask, or a local
thread/process pool — runs it. Around that sit four planes:

| plane | flowdapt implementation |
| --- | --- |
| **Graph** | `flowdapt/compute/resources/workflow/graph.py` — a `dict[str, OrderedSet[str]]` and a level-grouped topological sort. No node state, no per-node retry, no resume; rebuilt from the definition each run. |
| **Execution** | `flowdapt/compute/executor/ray/executor.py` — stage → `ray.remote(fn)` → `.options(**resources)` → `.remote()`. Fan-out via one detached `num_cpus=0` `MapperActor`. |
| **State handoff** | `flowdapt/compute/object_store.py` — `put/get/delete/exists` over a `Strategy` enum: a detached Ray actor holding `ObjectRef`s, or an fsspec directory-per-object artifact store, or "fallback" (try memory, else disk). |
| **Control** | `trigger_rule` resources (a JsonLogic-derived condition DSL over an in-process event bus, plus cron), and `config` resources bound to workflows by annotation selectors and **re-resolved on every run**. |

Two properties define it. First, **the executor is swappable** — that is the product's premise, and
it costs a `Broker(ABC)` whose only working implementation is an `asyncio.Queue`
(`flowdapt/lib/rpc/eventbus/brokers/rabbitmq.py:9` imports a symbol that does not exist; `aio-pika`
was never in `pyproject.toml` or `uv.lock`), a `BaseStorage` abstraction over TinyDB/Mongo with a
hand-rolled Alembic clone and Python-emulated transactions, and a `LocalExecutor` that
*diverges from production* on exactly the case the docs flag as dangerous
(`flowdapt/compute/executor/local/executor.py:173` returns a leaked loop variable where Ray returns
a list). Second, **durability is absent by construction**: `_run_workflow`
(`flowdapt/compute/resources/workflow/methods.py:275-294`) catches `Exception`, bumps a counter and
returns. "A workflow that dies at level 4 of 5 re-runs from level 0."

**rask is not that, and the difference is load-bearing.**

- rask **already chose its orchestrator**: Dapr Workflow is the only durable coordinator
  (`services/ingest/src/ingest/workflow.py` — parent → `enumerate_chunks` → child workflows →
  one Append commit → one terminal lineage event, with an error boundary routing every exit
  through one terminal step at `:279-342`). flowdapt's whole graph plane is a weaker version of
  what Dapr's history gives for free.
- rask **already has a declarative stage plane**: `packages/ratch/src/ratch/core/registry.py`
  (`Stage`, `ActorConfig`, `MediaGate` — frozen pydantic, `_shape_requirements` narrowing per
  `StageShape`) driving `packages/ratch/src/ratch/core/driver.py` over `lance_ray`.
- rask **already has a declarative record plane**:
  `packages/service-kit/src/service_kit/lakehouse/maintenance_policies.py` is a `kind/id/spec`
  record store with most-specific-wins resolution and an explicit ambiguity refusal
  ("never first-encountered-wins, which would let record ordering decide whose retention policy
  destroys whose version history (audit 2026-07-23)").
- rask **has no second store to keep coherent**: the catalog owns commits
  (`services/catalog/src/catalog/services/dataplane.py:641-687`, `read_version` CAS +
  `rask.ingest.run_id=` marker replay), NATS owns unit retry, Ray owns heavy compute, maintenance
  owns table health. flowdapt's object-store `Strategy` enum is precisely a second store with no
  version to order the two copies.

So the value here is **not** flowdapt's architecture. It is the **production tuition** — the scars
in `CHANGELOG.md` 0.1.43→0.1.52 and the comments the fixes left behind, most of which are about
Ray and land directly on rask's designated lance-ray seam.

---

## 2. THE ADOPT LIST — ordered by value for the bronze→silver design

### A1. Activity results and events carry identifiers and counts only — never payloads

**flowdapt:** `flowdapt/compute/resources/workflow/methods.py::_publish_workflow_run_event` —
`run.model_copy(update={"result": None})`, commented *"result can be arbitrarily large and gets
serialized on the event loop for every conditional trigger check."* Two earlier attempts failed at
the wrong boundary: an 8 MB `WORKFLOW_RUN_RESULT_CAP` measured with `pympler.asizeof` (2024-08-29),
then its deletion (2024-11-20). It took 18 months and a third attempt to move the fix from the
*storage* boundary to the *publish* boundary.

**rask has this leak, twice, and has already written down that it is a leak.**
`services/flows/src/flows/models.py:178-186` — `NodeResult.payload_text` is uncapped, and the
docstring says *"in the durable lane the uncapped payload enters the workflow history. Acceptable
while payloads are one page of text; bounding it (a blob handle instead of the bytes) is the
follow-up."* `open_dapr.md` §2.13 is the same defect at ingest scale (`enumerate_chunks` returns
the whole key set, carried a second time as each child's input). And the cost lands worse here than
in flowdapt: `open_dapr.md` §5.5(4) — history goes to *the single-replica CNPG Postgres that also
holds the lineage graph*.

The counter-example rask already got right is in the same tree:
`services/ingest/src/ingest/workflow.py::ChunkResult` returns `fragments: list[str]`
(FragmentMetadata JSON) and `errors: dict[str, str]` — handles and reasons, never bytes.

**rask shape.** A stated invariant for the bronze→silver workflow: activity inputs/outputs, Dapr
history, and published events carry **only** dataset URIs, version numbers, fragment-metadata JSON,
row-id ranges, counts and bounded error maps. Model the result on `ChunkResult`, not `NodeResult`.
**Seam:** the transform's result model + a gate in `tests/unit/test_ingest_invariants.py` beside
I4/A13 that AST-scans workflow modules and refuses a payload-shaped activity return.
**Effort:** medium. **Must not touch:** the catalog commit response and the terminal lineage
event — neither is workflow history.

### A2. Never pre-gate a Ray submission on logical resource labels

**flowdapt:** `_check_resources` docstring, 0.1.52 —
*"Custom resource labels are deliberately not gated here: they are logical labels an autoscaler can
satisfy on demand, and a worker type that advertises them may legitimately be scaled to zero
replicas at submission time. Gating on them against the static `ray.nodes()` snapshot would reject
such stages outright — the request would never reach Ray's scheduler, which is the component that
marks the task pending and triggers a scale-up."*

**rask is on the right side of this today and will be tempted off it.** There is no pre-submission
resource gate: `services/medallion/src/medallion/services/ray_submit.py` POSTs straight to
`/api/jobs/`. But `chart/values.yaml` runs KubeRay 2.56.1 with an autoscaler, and
`runners/htr/src/runner/pipeline.py:71-81` already documents fighting it
(*"Pools use `ActorPoolStrategy(size=N)` (fixed, autoscaler off)"*) — so "check the cluster has
GPUs before we submit" is exactly what someone will propose when bronze→silver starts queueing.
rask *also* has the accept-time-refusal reflex, correctly, for run **shape**:
`services/ingest/src/ingest/workflow.py:143-144` resolves sizing at ACCEPT *"so a refusal is a 400
rather than a drain that hangs"*, and `:207-234` refuses on `MAX_UNITS` *"HERE, before a single
task is published."* That instinct is right for run shape and wrong for cluster labels.

**rask shape.** A prohibition, stated at the submit seam
(`packages/ray-kit/src/ray_kit/submit.py`) and enforced by a structural test in the style of
`services/ingest/tests/test_poll_reason.py`: AST-parse the submit path and the new
Ray-coordination workflow body and assert `ray.nodes`, cluster-resource reads and GPU/label
preconditions appear nowhere. Accept-time gates may refuse on unit count, deadline, and FGA — never
on capacity. **Effort:** small.

### A3. Fold code identity into the deterministic Ray `submission_id`

**flowdapt:** two scars converge here. (a) The blue/green collision, 0.1.50 —
`self._mapper_actor_name = f"{base}-{uuid4().hex[:12]}"`, because a fixed-name detached singleton
*"is an implicit assumption that only one owner ever exists — deploys break that."* (b) The stage
cache key, 0.1.52 — widened to `(name, version, module, qualname)` because
*"two stages with the same name and source file but different target functions would otherwise
collide and run the wrong function."*

**rask's singleton is the submission id, and it is under-keyed on two axes.**
`packages/ray-kit/src/ray_kit/submit.py:53-60` — `submission_id(stage, token)` is
`re.sub(..., f"ray-{stage}-{token or 'notoken'}")`. Its caller passes `stage=settings.to_namespace`,
i.e. the literal string `silver`/`gold`
(`services/medallion/src/medallion/services/transform.py:277-284`), and the token is
`data.get("token") if isinstance(data, dict) else None` (`transform.py:94`). Therefore:

1. **Target collision.** Two projects cascading to `silver` with the same arrival token share one
   id; a **missing** token collapses every silver transform in the estate onto the single fixed id
   `ray-silver-notoken`. `submit_or_reattach` (`submit.py:86-112`) treats the collision as success
   and re-attaches — so the second transform silently never runs. That is flowdapt's
   "shares a key, runs the wrong function", on a governed table.
2. **Version collision.** The id carries no image tag. During a rolling deploy a redelivered
   trigger landing on the **new** pod re-attaches to a job the **old** pod submitted with the old
   entrypoint and `runtime_env` — running old code and reporting success. That is flowdapt's
   *"a stale actor keeps the OLD runtime_env"*, at job granularity.

**rask shape.** `submission_id(stage, token, *, target: str, code_version: str)` — hash the
resolved target dataset URI (and the source version pin, once bronze→silver exists) and the image
tag / chart `appVersion` the pod already carries; **refuse** a null token rather than substituting
`'notoken'` (a fixed shared id is the collision, not a fallback). **Seam:**
`packages/ray-kit/src/ray_kit/submit.py`, callers in `medallion/services/ray_submit.py` and
`transform.py:277`, plus an invariant in `tests/unit/test_invariants.py` asserting no submission id
is derived without a code-version component. **Must not touch:** `submit_or_reattach`'s
delete-and-resubmit-after-terminal-failure branch — that is the deterministic-id poison antidote
and is independently correct; and `packages/ray-kit/src/ray_kit/prune.py`'s terminal-only rule
stays the one job reaper. **Effort:** small–medium.

### A4. Stage identity on the Ray side — rask has none

**flowdapt:** `flowdapt/compute/resources/workflow/stage.py:114` —
`values["version"] = hash_file(getsourcefile(_fn))`, used as part of the remote-function cache key,
so a code change invalidates a cached export with no restart.

**rask's compute plane has no version-like field at all.**
`packages/ratch/src/ratch/core/registry.py::Stage` carries `name`, `shape`, `table`, `runner`,
`actor` — nothing else. `packages/ratch/src/ratch/core/runners.py:116-133::resolve_runner_actor`
resolves purely by name (`importlib.import_module(f"runners.{runner}.actor")`) and only checks that
`compute_factory`/`OUTPUT_SCHEMA` exist. Worse, `runner_env` is `@lru_cache`d on the **bare runner
name**, so editing `runners/htr/pyproject.toml` in a live process serves a stale `runtime_env`
forever — flowdapt's file-granularity mistake with the granularity dialled coarser.

There is a second, sharper consequence for bronze→silver. rask's resume is *"a property of the
read"* (`packages/ratch/src/ratch/core/driver.py:12-18`): `WHERE <column> IS NULL`, checkpointed
row ids, output-key diff. **None of those re-derive a row when the transform changes.** Fix an
embedding deriver and `col IS NULL` will never revisit the rows the old code already filled. The
estate half-knows this — the HTR path pins `RASK_HTR_MODEL_REVISION` — but the pin never reaches
the resume predicate.

**rask shape.** (a) A derived `Stage.identity` = `(stage.name, actor_module.__qualname__,
sha256(runner_env(runner)))` **[borrowed: flowdapt's *fixed* key, Apache-2.0 — take the 0.1.52 form,
not the original]**; re-key `runner_env`'s cache on the pyproject digest, not the name.
(b) Carry a transform version (source sha, or the pinned model revision) as a silver column or
fragment metadata written **in the same commit as the data** (the R26 `lineage`-column precedent in
`scripts/ray_stage_job.py`), and make the resume filter
`col IS NULL OR transform_version <> :v`. **Must not touch:** the catalog stays the only writer of
versions; backfill restartability stays Lance-native (`add_columns` batch_udf + `checkpoint_file`),
never the orchestrator's. **Effort:** medium.

### A5. Idle Ray workers are unreapable — `max_calls` is the only lever, and `.options()` drops it

**flowdapt:** 0.1.51 —
*"`num_workers_soft_limit` is not consulted by the raylet's idle-reaping path
(`TryKillingIdleWorkers` keeps ~`num_cpus_available` idle workers regardless), and workers reused
for back-to-back tasks never enter the idle pool to be reaped at all."* The lever is `max_calls`,
which *"must be set on `ray.remote(...)` — it is silently dropped if passed via `.options()`"*
(stated twice in comments, once in a docstring, with a source pointer). Cost admitted: worker
cold-start per task, so *"a small N (e.g. 4-8) can be used to amortize startup."*

**rask exposure.** `grep max_calls` over `packages/`, `services/`, `runners/` returns **nothing**.
`packages/ratch/src/ratch/core/runners.py:108::runner_ray_remote_args` returns a dict Ray Data
forwards as `ray_remote_args` — an `.options()`-shaped channel, so a `max_calls` added there would
be a silent no-op. `lance_ray.compact_files` / `compact_database` take `ray_remote_args` too
(`lance_docs/ray.md`), the same shape. And rask has already been OOM-killed once by an unbounded
Ray-side accumulation it did not know it had: `packages/ray-kit/src/ray_kit/prune.py:1-9` —
*"the live cluster reached 81,155 jobs / 164.7 MB per listing, which is what OOMKilled the compute
service."*

**Caveat, UNVERIFIED:** flowdapt diagnosed this on Ray 2.54.1. rask pins `ray>=2.56,<2.57`
(`runners/htr/pyproject.toml:21`, `chart/values.yaml` rayVersion 2.56.1). The raylet claim must be
**re-measured**, not inherited.

**rask shape.** (a) Refuse `max_calls` from `runner_ray_remote_args` — raise, don't drop — with a
comment naming the vendor gotcha. (b) Add an opt-in `tasks_per_worker: int | None` to
`ActorConfig`, default `None`, threaded onto the base `ray.remote(...)` in the job entrypoint,
enabled only on stateless task legs of the bronze→silver driver and gated behind an env knob with
the measurement recorded (the `MAINTENANCE_*_ENABLED` pattern,
`services/maintenance/src/maintenance/core/config.py:141,152,160`). **Must not touch:** the
`ActorPoolStrategy` pools in `runners/htr/src/runner/pipeline.py` or ratch's warm-client actors —
cold-starting a TrOCR actor per task is the regression the fixed pools prevent. **Effort:** medium.

### A6. Refuse new runs while draining — the flag exists, nothing reads it on the admission path

**flowdapt:** 0.1.49 shipped drain as a deploy primitive across nine files:
`context.flags["draining"]` with a typed 503 (`ServiceDrainingError`, `flowdapt/lib/errors.py:77`)
raised in `run_workflow`, an event-bus twin, and the trigger scheduler independently stopping.

**rask has the k8s half and the flag, and no reader on the admission path.**
`app.state.shutting_down` is set in nine lifespans (catalog, medallion producer + mover, lineage,
maintenance, viewer, search, annotator) and has exactly **one** reader —
`packages/service-kit/src/service_kit/probes.py:56`, which returns it on `/readyz`. Kubernetes
cooperates (`chart/templates/_helpers.tpl::lance.preStop`, `chart/values.yaml` preStopSeconds 5 /
terminationGracePeriodSeconds 30). But nothing refuses a new run, and — the sharp part —
`services/ingest`, `services/compute` and `services/flows` set the flag **nowhere** and mount
`service_kit.probes` **nowhere** (they ship liveness-only routers). Those three are the ingest
workflow host, the Ray client owner, and the graph runner. Worse,
`services/ingest/src/ingest/__init__.py:196-202` swallows a WorkflowRuntime start failure with a
warning and keeps serving — a pod that can never execute a run is Ready and takes POSTs.

Separately, **sidecar-delivered work does not traverse kube endpoints**: a `bindings.cron` POST and
a Dapr pub/sub delivery arrive on the app port regardless of readiness, so
`maintenance/api/routes.py::on_cron`, `compute/pruner.py::on_prune_cron` and
`lineage/api/reconcile_cron.py` each start a fresh unbounded pass inside the grace period.

**rask shape.** No `/drain` endpoint (a process-local flag cannot mean "this deployment is
draining" behind a multi-replica Service). Instead: one shared
`Depends(refuse_when_draining)` in `packages/service-kit/`, reading the **same**
`app.state.shutting_down` (the lifespan stays the only writer), applied in one commit to every
run-creating and sidecar-delivered route — `POST /v1/ingests`, `POST /produce`, `POST /train`,
`POST /flows/runs`, and every cron/subscription route. HTTP routes 503 with an RFC 9457 body (the
`RunRefused` shape at `services/flows/src/flows/models.py:103-115`); subscription routes return
`{"status": "RETRY"}`, **never** DROP. Plus: wire `service_kit.probes` +
`startup_complete`/`shutting_down` into ingest, compute and flows, with ingest's readiness a pure
read of `app.state.workflow_runtime is not None`. **Must not touch:** Dapr Workflow instances are
resumed by the runtime, not held by the pod — do not try to drain them; and readiness must not
probe a dependency (`services/ingest/src/ingest/health.py:1-20`: *"A liveness probe that fails when
a dependency is down turns one broken dependency into a restart loop"*). **Effort:** medium.

### A7. Never re-derive a carried value at the far end — and resolve the spec fresh at run start

**flowdapt:** M8 — the merged config crosses the process boundary as a **value** resolved once at
submission (`WorkflowRunContext`), so a mid-run config edit cannot affect an in-flight run; and M2 —
selectors are re-resolved by an **uncached** store read on every run, which *is* the entire
live-edit story.

**rask reached the same conclusion from the pain side, and wrote the bill out.**
`services/ingest/src/ingest/workflow.py:104-114` — `ChunkSpec.dataset_uri` used to be re-derived at
each end from env: *"Those are different datasets: workers wrote their fragments into one and the
lander committed against another, so every run would have committed an empty version while its
pages sat orphaned… Two derivations of one location is the bug; carrying the resolved value is the
fix."* Same for `ChunkSpec.sizing` (*"re-reading env inside the drain would let a rolling restart
change a live run's fragment size mid-fan-out"*) and
`services/flows/src/flows/models.py:209-216::NodeJob` (*"two derivations of one address is how a run
ends up split across two clusters"*). The uncached-read half rask also already runs:
`maintenance_policies.list_policies` is documented as "the sweep's per-tick load", no cache
anywhere.

**rask shape.** Combine both halves: the bronze→silver parent workflow's **first activity**
resolves the transform spec once (one registry read, no cache, no watch) and returns the **resolved
spec**, which is stamped into the workflow input and carried into every child, activity and Ray
actor via `fn_constructor_kwargs` / `runtime_env.env_vars`. Nothing downstream re-resolves, and no
Ray actor reads `os.environ` or constructs a `Settings()` to learn what the run is doing.
**Do not** adopt flowdapt's ContextVar (`stage_wrapper`'s `set_run_context`) — it buys nothing over
an explicit constructor kwarg and costs the reserved-`context`-parameter guard that only exists
because the ContextVar exists. **Effort:** small.

### A8. A declared `TransformSpec` record, validated for **vocabulary** at admission

**flowdapt:** the resource plane's shape is right (`kind`/`metadata`/`spec`, narrowed per kind) and
its validation is the scar: `TriggerRuleSpec.validate_rule`
(`flowdapt/triggers/domain/models/triggerrule.py:63-78`) checks only *shape* — operator names and
arity are never checked, so `{"equals": [...]}` is accepted with a 200 and then raises
`KeyError: 'equals'` on every event forever, visible only as a generic "Exception occurred".

**rask has the record store and the validation culture, separately.**
`packages/service-kit/src/service_kit/lakehouse/maintenance_policies.py` persists
`{"kind": "table"|"namespace"|"project", "id", "path"|"buckets", ...}` with `_record_is_well_formed`
narrowing per kind, `_key(kind, canonical_id)` deriving location from identity, and
`_policies/state/` as *"a separate prefix so the two writers never contend."*
`packages/service-kit/src/service_kit/lakehouse/features.py` makes `SUPPORTED` a whitelist with
*"deliberately no env escape hatch."* `services/flows/src/flows/catalog.py` is a server-declared
kind registry: *"a node kind whose execution is server-side… cannot be added by editing a Svelte
file."* And `open_ingest_design.md:891` already rules the replacement for the medallion lane guards:
*"a DECLARED SUBSCRIPTION on the source namespace — 'publications into `<proj>-bronze` wake lane X'.
Namespace-scoped, not table-scoped… `namespace` already has `can_update_properties` (writer rung)…
so this needs no model change."*

**rask shape.** A `_transforms/` record kind under the catalog control root, peer of `_policies/`,
reusing `maintenance_policies`' `_key`/`list`/`get`/`put`/`delete` shape. Written **only** through
an FGA-gated catalog action (`POST /v1/namespace/{id}/transform/set`, `can_update_properties`),
with the server doing an `exclude_unset` merge over the stored record and re-stamping identity
after the merge — the `services/catalog/src/catalog/api/v1/endpoints/policies.py::_record` shape,
whose docstring records what a wholesale union destroyed. Admission resolves the source namespace
and the target lane against a server-declared registry; **an unknown lane is a 422 naming the
offending key, never a 200.** Runtime state goes under `_transforms/state/`. Ship it **with**
(a) a reconcile category for orphaned specs in `services/maintenance/src/maintenance/services/
reconcile.py` and (b) a project-scoped list endpoint modelled on `ProjectPoliciesResponse`, because
flowdapt's S13 is that a silently-deleted config changes behaviour with nothing to point at.
**Must not touch:** the record only *describes*; the catalog stays the only commit writer.
**Effort:** medium.

### A9. Per-stage logical resource labels — declared, not free-form

**flowdapt:** `StageResources(BaseModel, extra="allow")` with every extra key coerced to float, and
`options = {"resources": stage.resources.extras(), "num_cpus": …, "num_gpus": …}`.

**rask has the typed half and not the labels half.** `ActorConfig`
(`packages/ratch/src/ratch/core/registry.py:46-56`) declares min/max actors, cpus, gpus, batch_rows
and `driver._map_batches` (`:158-172`) maps them one-to-one — but there is no way to pin a stage to
a **worker type**, which is exactly the lever bronze→silver needs on a heterogeneous cluster (HTR
GPU pool vs a CPU transform pool). CLAUDE.md states the current cost: *"the actor-pool sizes are
hardcoded literals in `runners/htr/src/runner/pipeline.py`… Retargeting hardware means editing all
three."*

**rask shape.** `resources: dict[str, float] = Field(default_factory=dict)` — an **explicit** field
with `extra="forbid"` preserved (flowdapt's `extra="allow"` is banned by
`services/flows/src/flows/models.py:49-57`, where `extra="ignore"` let *"ANY wrong-shaped body parse
as a valid empty graph"*). Threaded as `resources=stage.actor.resources or None` in `_map_batches`
and into `lance_ray`'s `ray_remote_args`. Pairs with **A2**: declaring the label must never mean
pre-checking it. **Effort:** small.

### A10. A bounded-by-bytes handle for oversized activity results

**flowdapt:** the config surface is two knobs and neither is a size threshold — memory-vs-disk is
per-call or global, never data-dependent. That absence is what made the payload leak (A1) a
three-attempt fix.

**rask has the threshold instinct on the Lance side and not on the workflow side.**
`services/maintenance/src/maintenance/core/config.py` bounds compaction memory as a *product* of
`MAINTENANCE_SCAN_BATCH_SIZE` (64) and `MAINTENANCE_COMPACT_THREADS` (2), because *"`compact_files`
used Lance's own 8192-ROW batch. Rows are not a unit of memory: against ~1.8 MB [page images] …"*
Bronze rows are page images by construction (`services/ingest/src/ingest/runtime.py:124-139`), so a
row-denominated batch on the bronze→silver read is the same mistake in a new place.

**rask shape.** One setting (`RASK_WF_INLINE_MAX_BYTES`, default **measured**, not guessed) applied
at the one place an activity result is built: under it the value rides history inline; over it the
activity writes to the run's staging prefix and returns `{"$handle": "<key>", "bytes": n}`. Two
call sites: `services/flows/src/flows/activities.py::run_node` and
`services/ingest/src/ingest/workflow.py::enumerate_chunks`. Generalise
`services/ingest/src/ingest/staging.py` from "fragments awaiting commit" to "per-run durable
side-channel" to host it — it already has the right properties (hash-of-the-work naming so retries
converge; `discover_staged` resolving by **exact cover** rather than trusting the listing; purge
only after commit lands). **Must not touch:** `stage_fragments`/`discover_staged`/`purge_staged`
keep their exact-cover semantics; a handle is workflow-history plumbing, never a governed artefact.
**Effort:** medium.

### A11. Timing uses a monotonic clock; the same number lands in the lineage facet

**flowdapt:** `EventBus._fire_callbacks` measures latency with `process_time_ns()` — **process CPU
time** — so every async callback that awaits I/O reports ~0 ms. A metric healthy by construction.

**rask states the rule in exactly one place.** `services/flows/src/flows/executor.py:236-240` —
`time.perf_counter` *"is the monotonic clock — a wall clock can step backwards under NTP and report
a negative duration."* The Ray side inherits OTLP config through `runtime_env` plus
`rk.trace_env()`, so a wrong clock there would be invisible in precisely the spans that matter (a
stage awaiting S3/Lance I/O).

**rask shape.** Every duration the transform emits — coordinator activity, Ray stage, commit —
uses `perf_counter`, rides `service_kit.setup_otel`, and the **same number** lands in the lineage
run facet so the graph and the metric cannot disagree. A CPU clock only for CPU-bound work, named
as such. **Effort:** small.

### A12. Boot-env vs live-spec, written down as two columns

**flowdapt:** M9 — two config planes sharing a word: the boot-time `Configuration`
(file < dotenv < env < CLI, frozen into a module global, restart to change) and the live
`ConfigResource`. Ray workers re-derive the boot plane from **env only**, never from the file.

**rask has both planes and has never drawn the boundary.**
`packages/service-kit/src/service_kit/config.py` — *"Read once at startup via `Settings()`… Never
re-read env vars in routes or services."* The live plane is the policy registry. But rask also has
env-derived worker paths (`packages/ray-kit/src/ray_kit/submit.py::trace_env`/`lineage_env`,
`packages/ratch/src/ratch/core/runners.py::runner_env`) and two module-import env reads that copy
flowdapt's shape exactly: `MAX_RUN_HOURS` and `MAX_UNITS` in
`services/ingest/src/ingest/workflow.py`.

Two riders. **(i)** Secrets appear in **neither** column — Dapr secret store only, fail-closed
(`services/medallion/src/medallion/mover.py`'s lifespan). Note the live counter-example:
`ray_submit.py:73` puts `S3_SECRET` into `runtime_env.env_vars` while
`scripts/ray_lance_job.py`'s own header warns *"NEVER put the token in runtime_env.env_vars — the
jobs API echoes runtime_env back."* **(ii)** Every bound is `int | None` with `None` meaning
unbounded — never 0, never -1. flowdapt's `run_retention_duration = -1` serialised itself into the
string `'-1'` and stopped meaning "never expire"; rask's own
`older_than_days: int = Field(default=7, ge=1)` carries the note *"`ge=1` (not 0): `timedelta(0)` is
falsy, so pylance collapses `older_than` to None and silently drops the threshold."*

**rask shape.** A two-column table in the bronze→silver design doc: BOOT-ENV (S3 endpoint, Lance
cache caps, OTLP endpoint, FGA store ids — worker-derivable, restart to change) vs LIVE-SPEC (target
table, batch size, actor sizing, enablement — must travel in the job payload per A7).
**Effort:** small.

### A13. Randomise / rotate iteration order, and count what a pass actually did

**flowdapt:** the `$ALL` callback's `for trigger in triggers:` loop has no per-item isolation, so
one malformed rule aborts every rule after it, in nondeterministic store order; and the scheduler
died silently on the first DB hiccup while `/health` stayed OK.

**rask has the per-item half right and the iteration half wrong, confirmed.**
Right: `services/maintenance/src/maintenance/services/sweep.py:249` — *"`compact_one` never raises
(it captures the per-dataset error)"*; `packages/ray-kit/src/ray_kit/prune.py:31-33` — *"one
undeletable job must not abort the other tens of thousands."* Wrong: `open_dapr.md` §2.19
(CONFIRMED) — `sweep.py:179` is a bare `for uri in uris:` over a deterministic listing with *"no
shuffle, no offset, no persisted cursor anywhere in the function. A pass that consistently dies at
dataset N never maintains anything after N, silently, forever"* — while the same module applies
`random.shuffle(failed)` 140 lines later for exactly this reason. And §2.20: `record_run()` fires
only **after** the loop, so *"a process killed at dataset 400 of 900 is observationally identical to
a tick that never arrived."*

**rask shape.** (a) Shuffle `uris` or persist a rotation offset in `sweep.py` (~10 lines).
(b) A `started` counter before the loop and a per-item swept counter inside it, emitted on **empty**
ticks too (the `service_kit/lakehouse/outbox_metrics.py::record_drained` rule — *"adding 0 CREATES
the series"*). (c) The subscription/transform fan-out gets per-record isolation and randomised order
from day one. `open_dapr.md` §4 makes (b) a **prerequisite** for any durability argument.
**Effort:** small.

### A14. Reuse the lance-ray global Pool instead of minting one per tick

**flowdapt:** 0.1.45 added a module-level `_instances` cache and `_actor_handles` map *"so we pay the
`ray.get_actor()` GCS round-trip only once"* — before that the backend map was rebuilt on every
call.

**rask has an open, named gap of the same shape.** `docs/RAY.md:87-89` records that lance-ray 0.5.0
ships `init_global_pool`/`set_global_pool` (*"upstream currently wires it to `vector_search` only"*)
and `docs/architecture/lance-ns-merge.md:462` (R27) lists "Ray Pool reuse" as an audit item.
`grep init_global_pool` over `packages/` and `services/` returns **only docs**. Maintenance hits it
hardest: the sweep is a cron running `compact_one` over every discovered dataset on a 120 s tick.

**rask shape.** When distributed compaction lands (`docs/architecture/lance-ns-merge.md:511` — the
CONFIRMED gap: `ds.optimize.compact_files` in-process where `lance_ray.compact_files` distributes),
call `init_global_pool()` once in the maintenance lifespan and `clear_global_pool(close=True)` on
shutdown. **Must not touch:** the pool belongs to the process that owns the lane — maintenance owns
table health; the ingest/medallion planes never reach into it. **Effort:** medium.

### A15. Bind in-process single-flight claims to their chart replica count

**flowdapt:** no leader election, and the same service has a fan-out-safe path (competing consumers)
and a fan-out-unsafe path (the scheduler) with **nothing in the code marking the difference**.
`flowdapt/compute/executor/dask/executor.py:317` still carries
`# TODO: Services will likely require some type of leader election`.

**rask has four individually-correct answers and zero machine checks tying any of them to the
chart.** `maintenance/api/routes.py`: a module `asyncio.Lock`, *"with `compactionReplicas=1`
(values.yaml) this is cluster-wide single-flight."* `medallion/services/transform.py`:
`_write_lock`, *"With `moverReplicas=1` (the default) this is `maxConcurrency=1` for the stage
cluster-wide."* `lineage/api/reconcile_cron.py`: a real cluster-wide advisory lock because *"the
cron fires on EVERY lineage replica independently."* `service_kit/control_events.py`: the catalog
subscribes *"WITHOUT a `queueGroupName`, so every replica receives every event."* **One
`values.yaml` bump to `moverReplicas: 2` turns two of those comments into lies with no test
failing.** Note the mover's own mitigation — *"the write stays overwrite-idempotent"* — is exactly
the property that will **not** hold for a lance-ray transform writing fragments then committing.

**rask shape.** Assertions in `tests/unit/test_invariants.py` (which already runs 30+ chart-vs-code
gates) binding each in-process single-flight site to its replica count: an `asyncio.Lock` claiming
cluster-wide single-flight requires `replicas: 1`, or a real distributed lock like lineage's. Pure
test work — no runtime change, no writer. Related, and worth the same commit: assert every
Deployment probe path resolves to a route the app mounts (rask shipped `/api/health` in the chart
against an app that mounted no such route — `services/ingest/src/ingest/health.py:1-20`), and that
every chart-set `RASK_*` env has a reader (`RASK_INGEST_MAX_RUN_HOURS` was declared, read by
nothing, and gate A15 asserted a relation over it and **passed** — *"a green gate over an unenforced
relation is worse than no gate"*). **Effort:** medium.

### A16. One `transform_batch` callable, two drivers, one drift pin

**flowdapt:** `execute_workflow` is a real harness driving the same `Executor.__call__` production
uses — and it **diverges** anyway: `LocalExecutor.__call__` returns a leaked loop variable where Ray
returns a list, and its `map_inner` is a sequential `for`, so *"`execute_workflow` gives you no
signal about fan-out concurrency, resources or GPU packing at all."*

**rask has both halves separately.** Good: `services/flows/src/flows/executor.py:1-10` — `dispatch()`
is shared by the inline lane and the Dapr activity because *"a node that behaves differently
depending on which orchestrator called it is a node whose sandbox result means nothing."*
Warning: `scripts/ray_stage_job.py` has **two** paths for one contract (TABULAR via distributed
lance_ray, MEDIA via a driver-side pylance round-trip, because *"lance_ray's write strips blob
typing"*), held together only by a drift-pin unit test keeping the inlined deriver byte-identical to
`medallion.services.media` (`tests/unit/test_ray_stage_job.py`). That drift pin is rask's answer to
flowdapt's divergence scar and it is the better one.

**rask shape.** Build bronze→silver as **one** `transform_batch(source_uri, target_uri, spec) ->
Result` that both the Ray job entrypoint and the local test drive, pinned by a drift test beside
`test_ray_stage_job.py`. Its docstring states what the local lane **cannot** certify: fan-out
concurrency, `resources` label satisfaction, GPU packing, and lance_ray's blob-typing behaviour are
cluster properties (CLAUDE.md: *"Verify like it ships"*). Test contract: exercise storage against
moto or a real local `.lance` dir, never a mocked sink; test every lane branch end-to-end; fuzz any
selection/ordering logic against a brute-force oracle (the `staging.py::_exact_cover` precedent,
which *"raised on 24% of inputs that had a perfect cover"*); and **no behaviour claim may be
discharged by a signature or annotation check** — `open_dapr.md` §2.2 records rask doing exactly
that once. **Must not touch:** do not add a third in-process implementation; the medallion's
`MEDALLION_RAY_ENABLED` in-process path already exists. **Effort:** medium.

### A17. Bound Ray dashboard reads with a background cache, not a live call per request

**flowdapt:** 0.1.46 — `ray.nodes()` was on the hot path of `environment_info()`, which the 10-second
health loop called. Moved to a 30 s background `_refresh_nodes` writing `_nodes_cache`, with
`_check_resources` failing **open** on an empty cache.

**rask bounds at the source and still reads live per request.**
`packages/ray-kit/src/ray_kit/dashboard.py:49-68` caps `MAX_JOBS=200` and `MAX_TASKS=500` with the
measurements attached (81,155 jobs / 164.7 MB; 1179 MiB peak against a 1536 MiB limit; the task
endpoint *"polled every 5 s by two separate pages"*). But
`services/compute/src/compute/routes.py:42-58` still makes `/ray/cluster`, `/ray/actors`,
`/ray/tasks`, `/ray/overview` live dashboard round-trips per request. rask has half the discipline
already: `services/compute/src/compute/health.py` keeps `/health` off Ray on purpose.

**rask shape.** Keep bounding at the source as the first lever. If a cache is added, it is **one
owner** — compute — writing `app.state.ray_cluster_cache` from a **Dapr cron binding**, not an
in-process refresh task (`services/compute/src/compute/pruner.py:1-14`: *"The schedule lives in the
chart's Component — no scheduler thread in the service"*; and
`tests/unit/test_ingest_invariants.py:173::test_a13_no_completion_polling_survives` is a repo-wide
gate against in-process polling). `/ray/health` stays a live but bounded call. **Effort:** medium.

---

## 3. The Ray production scars as a lance-ray CHECKLIST

Each row is a flowdapt scar and the rask action. Tick these before the bronze→silver transform
ships.

| # | Scar (flowdapt) | rask must |
| --- | --- | --- |
| R1 | `ray.remote(func)` mints a fresh UUID → a **permanent** GCS function-table entry, never evicted (0.1.49) | **Keep the driver ephemeral.** Every rask Ray driver today exits with its job (`scripts/ray_stage_job.py`, `runners/htr`'s CLI), so the table dies with it — but that is an *accident*, not a stated choice. Write it down in the lance-ray seam doc, and forbid `ray.init`/`ray.remote` in any `services/*` module. |
| R2 | Caching the post-`.options()` object was the *fix to the fix*: two stages sharing a key but different resources collided, *"whichever ran first after a restart freezing its resources onto the others"* (0.1.52) | If a long-lived driver is ever adopted: cache **only** the base `ray.remote(cls)` keyed on `Stage.identity` (A4), apply `.options()` per submission. `ActorConfig` is exactly the parameterization that must not be baked in. |
| R3 | `max_calls` is **silently dropped** by `.options()` | Refuse it from `runner_ray_remote_args` (raise, don't drop). See **A5**. |
| R4 | Idle workers unreapable on 2.54.x; `num_workers_soft_limit` is not consulted | Re-measure on 2.56 (**UNVERIFIED** for rask's pin). Opt-in `tasks_per_worker`, stateless legs only. See **A5**. |
| R5 | Preflight resource checks rejected work the autoscaler would have satisfied (0.1.52) | Never gate on labels. Structural test. See **A2**. |
| R6 | Ray Client `.remote()` **freezes the process** when the connection drops (ray#21419) — thread-boxing alone is insufficient, needs `asyncio.timeout` too | Non-issue *because* rask never uses Ray Client: services speak Jobs REST / dashboard HTTP with explicit timeouts (`ray_request_timeout_seconds`), job drivers run in-cluster with `address="auto"`. **Keep it that way** — a `ray://` client must never appear in a `services/*` process. |
| R7 | `ObjectRef.future()` blocks in Client mode (`_wait_for_id`); `lazy()` submitted synchronously on the loop (0.1.43, 0.1.47) | Already law in rask (`anyio.to_thread.run_sync` at every seam: `ray_kit/dashboard.py:5`, `compute/lifespan.py:29`, `compute/pruner.py:44`, `storage/s3.py:16`). **Extend it:** `lance_ray.read_lance`/`write_lance`/`compact_files` are blocking *driver-side* calls — from a FastAPI handler they go through `run_in_threadpool`; better, maintenance submits a Ray **job** rather than importing lance_ray at all, keeping `ray` out of the mover/maintenance images (`ray_submit.py`: *"no ray package in the mover image"*). |
| R8 | Blue/green: a fixed-name detached singleton assumes one owner; deploys break that (0.1.50) | rask has no detached actors — but the deterministic `submission_id` is the same singleton. See **A3**. If a named cluster-scoped resource is ever added, name it per driver generation and reap via the existing `prune_jobs` cron — **never** a second reaper. |
| R9 | Orphan reaping must spare actors owned by a still-running driver; best-effort, skipped if the state API is down | rask already got this right: `prune.py` deletes only `TERMINAL_STATUSES` — *"PENDING/RUNNING are never candidates: deleting live work is not retention, it is sabotage."* No change. |
| R10 | `py_modules` cannot go in an actor's `runtime_env` — only `ray.init()` can serialize live module objects | Non-issue: rask's `runtime_env` is env-vars-only; code arrives via the Dagger-built Ray image. **Preserve the invariant** — nobody "fixes" a missing dependency by uploading it at submit time. And note the live violation to fix separately: `ray_submit.py:73` puts `S3_SECRET` in `runtime_env.env_vars`, which the Jobs API echoes back. |
| R11 | The nested-task anti-pattern: a remote task doing `ray.get` on its sub-tasks pins a whole worker per map call | Review rule for the driver: **collection happens in the driver** (`ratch/core/driver.py`: *"actors compute, the driver commits"*), never in a nested remote task; no synthetic throttle resource injected at init. rask's knowingly-paid version is `transcribe_concurrency_serve = 8` (`runners/htr/src/runner/pipeline.py:107`) — eight workers deliberately blocked on a Serve handle, and `:64-73` records how that becomes the throughput ceiling. |
| R12 | Object ownership must move off the producing worker (`ray.put(value, _owner=actor)`), and the ref must be list-wrapped to defeat auto-dereference | rask is immune by design — *"heavy blobs never transit Ray Data blocks; blob stages ship only `_rowid`s"* (`driver.py:74-79`). This is **why** enabling `max_calls` (R4) cannot orphan a payload. Record the two mechanics as an R27 note so they are recognised if someone proposes an in-memory stage handoff. |
| R13 | `RAY_CLIENT_RECONNECT_GRACE_PERIOD` forced at **package import time**, because it must precede client construction | Some Ray knobs must be set before the driver exists. They go at the top of the job entrypoint with a comment saying why they cannot be a `Settings` field — **never** an import side effect in `packages/*` or `services/*`. |
| R14 | Health endpoints must not talk to Ray (0.1.46) | See **A17**. Also: `services/compute` currently mounts no `/readyz` at all — if one is added and touches Ray it needs `asyncio.timeout` + fail-closed, or better, does not touch Ray. |
| R15 | `_running_workflows` is typed but extended with bare refs; GROUP_BY_GROUP registers nothing, so its in-flight stages are never cancelled on shutdown | rask's non-cancellation is deliberate and documented (`ray_submit.py:95-106`: *"A job that dies commits nothing and rings nothing; the lineage reconciler catches it against storage truth"*). **State it as a property** of the new durable-wait design rather than leaving it as an oversight: shutdown cancels nothing Ray-side; the deadline leg of `when_any` produces a FAILED terminal outcome through the one terminal step; abandoned jobs are reclaimed by `prune_jobs` + the reconciler. Note `open_dapr.md` §3: the losing leg of a `when_any` cannot be cancelled, so the timer record persists. |
| R16 | Ray worker RAM was bounded by two knobs whose **product** is the memory | Direct rask precedent on the Lance side: `MAINTENANCE_SCAN_BATCH_SIZE` × `MAINTENANCE_COMPACT_THREADS`, because *"the memory is their PRODUCT and bounding one alone bounds nothing"*, and *"Lance's `num_threads` defaults to the machine's parallelism, which is the HOST's core count, not the pod's `limits.cpu: \"1\"`."* Size the bronze→silver read in **bytes**, not rows — bronze rows are page images at ~1.8 MB. |

---

## 4. The SKIP list — one line each, so nothing is re-litigated

**Architecture / lifecycle**

| Idea | Killed by |
| --- | --- |
| `Service` ABC + `ServiceController` gathering startup/run/shutdown TaskSets | rask has no in-process supervisor: one FastAPI app per service under uvicorn+tini; the k8s Deployment *is* the supervisor (`.docker/ingest.dockerfile:91-95`). |
| AsyncExitStack auto-entering any duck-typed async CM in the context | `service_kit`'s `LifespanFactory` keeps enter/exit **ordered**, and flows states why (`http.aclose()` before `runtime.shutdown()`, or pooled connections leak). |
| `ApplicationContext` global + name-or-annotation DI, `__getattr__ → None` | A silently-None dependency is the exact FGA bypass ingest already guards against by building the client eagerly (`ingest/__init__.py:52-57`). |
| `POST /drain` + `drain_on_sigterm` + 300 s poll-until-idle | A process-local flag cannot mean "this deployment is draining" behind a multi-replica Service; k8s owns endpoint removal + the two-stage kill; Dapr Workflow survives pod death by replay. (The *admission* half is adopted — **A6**.) |
| `NoSignalServer` monkey-patch + `loop='none'` | The receipt for the in-process supervisor rask is not building; uvicorn is PID-1's child, not a hosted object. |
| `flowdapt.plugins` entry-point discovery | rask's extension seams are in-tree and test-gated (invariant I1 / gate A9); an entry point lets an installed package change admissible sources with no diff. |
| `install_plugin()` over HTTP / runtime plugin management | Abandoned upstream with zero callers; rask's model is Dagger build → rollout → discover at startup. |
| `import_from_string` + `reload()` per resolution | Contradicts immutable images and re-executes module top levels inside Ray actors. (The *source-hash* half is adopted — **A4**.) |

**Event / trigger plane**

| Idea | Killed by |
| --- | --- |
| The `check_condition` first-key DSL (eq/ne/gt/and/or/var) | `service_kit/control_events.py:16` — *"An event is a refresh hint, never authoritative data"*; and `open_ingest_design.md:843` collapses the trigger surface to "a table's `published` tag advances". |
| `var` dot-path returning a None sentinel | rask's cascade **DROPs** on an unresolvable selector with a counter (`transform.py:134-141`), because falling back *"would transform the WRONG tenant's data while emitting real-looking lineage for it."* |
| A `$ALL` wildcard callback querying the rule store per event | rask routes at the broker (per-mover app-id + sub_topic); a control-bucket read per delivery would also blow the 30 s Dapr ack window. |
| An in-process 5 s cron poll loop | Schedules are `bindings.cron` chart Components — *"no scheduler thread in the service."* |
| `Broker(ABC)` with memory/rabbitmq backends | Dapr **is** the abstraction; the one direct-NATS module is a named, test-counted exception (invariant I3). Upstream's RabbitMQ broker has been unimportable since the initial commit. |
| `EventBus` + `EventStream` + sequential callbacks | Fan-out is `wf.when_all` over child workflows; publishing goes through the one `dapr_publish` seam with three invariant tests behind it. |
| Actions as arbitrary dotted import paths | Dispatch is a closed server-declared registry (`flows/catalog.py`); a client-supplied import path is remote code execution with extra steps. |
| Class-level mutable broker subscription state | No in-process broker; rask's singletons are deliberate and keyed (`shared_lance_session()` keys on `(uri, version, etag)`). |
| No auth on the trigger API | Every cron/subscription route is `Depends(require_dapr_token)`; every catalog mutation is FGA-gated. rask cannot express this bug. |

**Compute / graph**

| Idea | Killed by |
| --- | --- |
| The level-grouped topological sort | `services/flows/src/flows/graph.py::topo_waves` already does it with **sorted** waves *"so the plan is byte-identical across processes and across a workflow replay"*, named self-loops, deduped edges, and no `assert`. |
| Positional `depends_on` + "last group is the return value" | `RunState.nodes` is keyed by node id — "what did the run produce" is never "whatever ran last". |
| `ParameterizedStage`'s `args.pop(0)` iterable + `options: dict` bag | The fan-out unit is a typed `ChunkSpec`/Ray Data block carrying run identity; an `options` bag that swallows first-class fields is the `extra="ignore"` false-all-clear defect. |
| The detached zero-CPU `MapperActor` | Would be a **fourth** fan-out owner: Dapr owns DAG fan-out, Ray Data owns intra-job, JetStream owns unit distribution. Also `list(iterable)` materialises everything before submitting. |
| `allow_partial_failure=True` dropping exceptions silently | rask decided the opposite in three places: `ChunkResult.errors` keyed by unit, `NodeRunState(status="failed")`, and `DatasetResult`'s skipped/refused/error trichotomy — *"Folding a refusal into either is what made a shallow clone's silent full materialization invisible."* |
| Pickling a `WorkflowRunContext` into every task + a worker ContextVar | The value-not-lookup half is adopted (**A7**); the ContextVar is not — explicit `fn_constructor_kwargs` beats ambient state and needs no reserved-name guard. |

**Object store / resource plane**

| Idea | Killed by |
| --- | --- |
| The `Strategy` enum dispatcher (memory / artifact / fallback) | No second tier exists: the handoff medium is a catalog-governed commit, and `test_i4_only_the_lander_writes_lance` (`LANDER_ALLOWED = {"lander.py"}`) makes one-writer a compiled gate. |
| Cluster memory as a detached actor holding `ObjectRef`s | Would be a second long-lived state owner outside the catalog, betting the design on the private `_owner=` kwarg. (Mechanics recorded under **R12**.) |
| Directory-per-object artifacts + `.artifact.json` sidecar | Already built, better: `services/ingest/src/ingest/staging.py` — hash-of-the-work naming, exact-cover ownership, purge-after-commit. |
| Save/load hooks + self-declared `value_type` + `partition-{i}.parquet` read in `fs.ls` order | Schema is Lance's and the catalog's; `_exact_cover` is *"deterministic by construction… S3 listing order is not a choice"*; `artifact.clear()`-then-write is the ordering `purge_staged` forbids. |
| Put-then-fallback stale reads | Two stores of one fact need a version to order them; if you cannot name the version there may be only one store. |
| Module-level `_actor_handles` / `_instances` caches | rask already caches the expensive handles where they belong (`app.state.ray_client`, actor `__init__`). (The lance-ray Pool case is adopted — **A14**.) |
| Annotation selectors (OR-matching, `partial()`) | `resolve_policy` is most-specific-wins over the real containment tree, with an explicit ambiguity refusal (audit 2026-07-23). |
| Shallow `a | b` merge with broad-over-narrow precedence | Catalog policy writes use `exclude_unset` merge (a wholesale union once *"silently clear[ed] `scan_batch_size`"*), and reads never synthesize a merged view — *"a surface rendering the three tiers as an effective, merged policy is lying."* |
| Client-side `flowctl apply` (GET → branch → PUT) | Writes are server-side, FGA-gated and CAS-guarded via `read_version` + the `run_id` transaction marker. |
| `Annotated[..., Immutable]` walked at merge time | pydantic `frozen=True` for values; the server re-stamps identity after the merge (`policies.py::_record`), so there is no "unset immutable can be written once" hole. |
| Reserved `flowdapt.ai/*` annotation re-injection | rask splits **keyspaces** instead: `_policies/` vs `_policies/state/`, *"a separate prefix so the two writers never contend."* |
| A pluggable document DB + hand-rolled migrations/transactions/query AST | The app's relational DB died at P7a; control records are JSON under the control root, schema-evolved by pydantic defaults. Upstream's own `add_index` is a `pass` stub. |
| Vendor-media-type per-kind API versioning | The catalog is Lance-Namespace-conformant (`/v1` in the path, 24-code problem+json); rask pins shape with `version: Literal[1]` (`CatalogResponse`). |
| pydantic v1/v2 compat shim + upper bound | pydantic-v2-only; the one v1 contact (the Ray Job SDK) is behind a structural `Protocol` at the package edge. |
| `simplefilter('ignore', UserWarning)` in a library module | rask surfaces unactionable signals as counted, returned fields; a global filter in `packages/*` would silence pylance/lance-ray deprecations across every worker. |
| Name uniqueness enforced only on insert; `uid | name` lookup | Storage key is derived from identity (`_key = sha256(f"{kind}:{canonical_id}")[:24]`); there is no rename verb and no second lookup axis. |
| A bare `assert` as a type guard | `Literal` discriminators on pydantic models; ruff's assert rule holds outside `tests/`. |
| Fossils left in place (dead `__main__`, stale JsonLogic comments, commented-out `gather_with_concurrency`) | rask runs the same pass under its own name (`open_dapr.md` §2.22 "Claimed and did not survive"). One concrete application: retire **both** medallion lane guards in one commit (`ingest_trigger.py:53-58` **and** `transform.py:110-124`) — deleting one *"moves the drop one hop later and changes nothing."* |

---

## 5. Sketch: the bronze→silver transform

How the surviving ideas compose. This is the artifact the next design session starts from, not a
spec — every number below is a placeholder until measured.

### 5.1 The four owners, unchanged

```
catalog     — owns the commit (read_version CAS + rask.ingest.run_id= marker replay)
NATS        — owns unit retry (WORK_QUEUE, ack/nak, maxDeliver)
Ray         — owns heavy compute (lance_ray read/write, actor fan-out, scheduling)
maintenance — owns table health (compaction, cleanup, refusal gates)
Dapr WF     — owns durable COORDINATION: submit -> durable wait -> react. Nothing else.
```

The transform adds **no fifth owner**. It is a coordinator + a job.

### 5.2 The record: `_transforms/` (A8)

```
<control_root>/_transforms/<sha256(kind:canonical_id)[:24]>.json
<control_root>/_transforms/state/<...>          # written only by the executing side
```

`TransformSpec` — frozen pydantic, `extra="forbid"`, no `options` bag:

```
source_namespace   target_namespace   target_table
lane               # enum over server-declared lanes; unknown -> 422 at admission
actor: ActorConfig # + resources: dict[str, float]  (A9)
batch_bytes        # NOT batch_rows (A10 / R16)
enabled: bool
```

Write door: `POST /v1/namespace/{id}/transform/set`, `can_update_properties`
(`open_ingest_design.md:979` — no FGA model change needed). Server does `exclude_unset` merge and
re-stamps identity. Ships **with** a reconcile category and a project-scoped list endpoint.

### 5.3 The workflow body

```
transform_run(ctx, req):
    spec        = yield ctx.call_activity(resolve_spec, req)          # A7: resolve ONCE, fresh
    partitions  = yield ctx.call_activity(plan_partitions, spec)      # returns POINTERS (A1/A10)
    for wave in waves(partitions):                                    # durable checkpoint per wave
        results = yield wf.when_all([ctx.call_child_workflow(partition_run, p) for p in wave])
    yield ctx.call_activity(finalize, ...)                            # ONE Append commit
    # every exit routes through emit_terminal — the ingest error-boundary shape
```

Rules inherited verbatim from `services/ingest/src/ingest/workflow.py`:

- **Replay-safe clock only** — `ctx.current_utc_datetime`, `ctx.create_timer`. Never
  `datetime.now()` in the body. (flowdapt's trigger service fed naive `utcnow()` to a scheduler with
  `default_utc=False`; rask's version of that trap is `sweep.py:178`, harmless only because the
  sweep is not a workflow.)
- **No httpx, no clock, no uuid in the body.** `submission_id` derives from `ctx.instance_id` +
  payload (`open_dapr.md` §5.6), never minted inline.
- **One terminal step.** Every exit — success, chunk exhaustion, deadline — goes through
  `emit_terminal` (`workflow.py:327-337`), because a chunk that raised straight out of `when_all`
  once killed the workflow before the FAIL record and the queue release.
- Mid-run progress via `ctx.set_custom_status(...)` — no side ledger (A1's read-side twin).

`partition_run` is one activity: `submit_or_reattach` → **durable wait** on a bounded
`GET /api/jobs/{sub_id}` status activity racing a deadline timer (`when_any`) → react. The losing
timer leg cannot be cancelled (`open_dapr.md` §3) and the Ray job is **not** cancelled on shutdown
(**R15**) — both are stated properties, not oversights.

### 5.4 The job

One `transform_batch(source_uri, target_uri, spec) -> Result` (A16), driven by the Ray entrypoint
and by the local test, drift-pinned.

```
lance_ray.read_lance(source_uri, columns=[_rowid, ...])   # row ids only; blobs stay put (R12)
  -> map_batches(actor_cls,
                 concurrency=(spec.actor.min_actors, spec.actor.max_actors),
                 num_cpus=..., num_gpus=..., resources=spec.actor.resources or None)  # A9
  -> driver collects fragment metadata (never payloads)    # "actors compute, the driver commits"
  -> catalog commit: read_version + run_id marker          # one writer
```

Resume is a property of the read, **plus** the transform version (A4):
`WHERE col IS NULL OR transform_version <> :v`. Column backfill stays Lance-native
(`add_columns` batch_udf + `checkpoint_file`) — the orchestrator schedules it and never owns its
restartability.

### 5.5 What the "object-store strategy" idea becomes

Not a strategy. **One threshold, one direction** (A10):

```
activity result  <= RASK_WF_INLINE_MAX_BYTES  ->  rides Dapr history inline
                 >  threshold                 ->  written to staging_root(dataset_uri, run_id),
                                                  returns {"$handle": key, "bytes": n}
```

`staging.py` hosts it — generalised from "fragments awaiting commit" to "per-run durable
side-channel", keeping hash-of-the-work naming and purge-after-commit. There is no `get` by key from
another run, no `exists`, no fallback tier. flowdapt's put-then-fallback stale-read trap is
unreachable because there is only ever one copy.

### 5.6 What the "parameterized stage" idea becomes

Not `args.pop(0)`. **Three fan-out layers, each with one owner:**

```
Dapr Workflow  ->  waves of partitions (durable, replayable, survives the pod)
Ray Data       ->  rows within one partition (streaming, resource-aware)
JetStream      ->  redelivery of a failed unit (retry, maxDeliver, DLQ)
```

Per-item failure is carried, not dropped: the partition result is `ChunkResult`-shaped —
successes counted, failures as identified `key -> reason` pairs — and `finalize` keeps its refusal
to commit an empty fragment list. **A run that dropped 3 of 400 partitions must never report
COMPLETE.**

### 5.7 Preconditions before any of this is built

1. **Measure** the current `enumerate_chunks` result size at advertised scale
   (`open_dapr.md` open question #8 — the 120 MB figure is the doc's own arithmetic, **UNVERIFIED**).
   A10's threshold default depends on it.
2. **Land the counters** (A13) — `open_dapr.md` §4 makes them a prerequisite for any durability
   argument, because *"nobody can currently measure how often a pass is actually lost."*
3. **Re-measure** the Ray 2.56 idle-worker behaviour (R4) before adding `max_calls`.
4. **Fix `submission_id`** (A3) before a second concurrent transform target exists —
   `ray-silver-notoken` is a live collision today.
5. **Verify like it ships** (CLAUDE.md): a green local `transform_batch` certifies nothing about
   fan-out, GPU packing, `resources` satisfaction, or lance_ray's blob-typing. The kill test is
   `open_dapr.md` §5.7 Test B — kill the Ray head and confirm the workflow *observes the job
   vanish* rather than hanging until the timer.
