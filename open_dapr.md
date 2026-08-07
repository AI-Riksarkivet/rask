# open-dapr — the Dapr plane: what is wired, what is broken, and whether Workflow should own Ray jobs

Working plan, **2026-08-07**, revised after an adversarial verification pass. Unsettled work;
this file is deleted when it lands. `docs/` is for settled architecture only.

**This revision.** Every finding in §2, §3 and §4 was put through an adversarial re-read against
`HEAD 50e5b684` — each cited `file:line` opened, the vendored SDK under
`.venv/lib/python3.13/site-packages/dapr/` read for every SDK claim, `ty` actually run for the
annotation item, and the live k3s cluster used for §2.21. **35 claims survived, 2 did not.** The
two that did not are in §2.22, one line each, so they are not re-filed. Three §2 items from the
first draft (`flows` never reads workflow state, no terminate/purge surface, `MAX_RUN_HOURS` as a
workflow-scope env read) were **not in any verification slice** — they are held in §2.23,
neither confirmed nor refuted.

**Evidence convention.** Every claim carries one of four markers, and they are not
interchangeable:

- `(live 2026-08-06)` / `(live 2026-08-07)` — **measured against the running k3s cluster**.
- `path:line` — **read from source** (chart templates, Python, the vendored SDK). Read, not
  executed.
- `UNVERIFIED` — an inference, an estimate, or arithmetic. Never treat one as a measurement.
- **Verdict / Severity** — the outcome of the adversarial pass. `CONFIRMED` = every cited fact
  and the consequence reproduce. `PARTLY` = the mechanism reproduces, a stated consequence does
  not; the narrowing is written into the entry.

**Scope.** The three Dapr building blocks in use (Workflow, state/secret stores + actors,
pub/sub + bindings), plus one open design question: should Dapr Workflow own Ray job
submission. Ray's own fault-tolerance guarantees were read from `docs.ray.io` and are quoted
where they bound a decision.

---

## 1. What is wired today

### 1.1 App-ids on the cluster

Sixteen, all live (live 2026-08-06):

| app-id | plane | Dapr surface in use |
| --- | --- | --- |
| `ingest` | ingest | **Workflow host** (`services/ingest/src/ingest/__init__.py:190-193`, registered unconditionally in the lifespan), state store, pub/sub |
| `flows` | flows | **Workflow host**, but only when `DAPR_GRPC_PORT` is set (`services/flows/src/flows/lifespan.py:49`) |
| `annotator` | annotation | **Actors** ×3 (`projects/actor.py`, `project_actor.py`, `tenant_actor.py`), state store, reminders |
| `catalog` | governance | state store (user-state documents), pub/sub publish + a broadcast control subscription (`services/catalog/src/catalog/api/dapr.py:84`) |
| `lineage` | governance | pub/sub subscribe (`services/lineage/src/lineage/api/dapr.py:70`), DLQ parking route (`:77`), cron binding, secret store |
| `maintenance` | lakehouse ops | **two cron bindings** (`chart/templates/maintenance.yaml:9`, `:26`), pub/sub publish under flags, secret store |
| `lance-ray` | medallion **producer** | pub/sub publish + two cascade heads (`services/medallion/src/medallion/api/bronze_arrival.py:37`, `:65`), train trigger (`api/train.py:101`) |
| `bronze-to-silver` | mover | pub/sub sub `medallion.bronze` → pub `medallion.silver` (`chart/values.yaml:794`) |
| `silver-to-gold` | mover | sub `medallion.silver`, terminal (`pubTopic: ""`, `chart/values.yaml:795`) |
| `media-to-silver` | mover | sub `medallion.media`, terminal (`chart/values.yaml:802`) |
| `pages-to-gold-htr` | mover | sub `medallion.bronze`, terminal (`chart/values.yaml:811`) |
| `gateway` | edge | **service invocation** — routes leave through its own daprd |
| `compute` | compute | service invocation target |
| `controlplane` | control | service invocation target |
| `search` | explorer | service invocation target |
| `viewer` | explorer | secret store (per-store S3 credentials, `services/viewer/src/viewer/api/v1/endpoints/objects.py:70`) |

### 1.2 Components

Fourteen (live 2026-08-06):

| component | type | scoped to | evidence |
| --- | --- | --- | --- |
| `lance-secrets` | `secretstores.hashicorp.vault` | hardcoded branches **plus** `stateStore.scopes` (concat) | `chart/templates/dapr-component.yaml:212-219` |
| `lance-statestore` | `state.postgresql`, `actorStateStore: "true"` | `annotator`, `catalog`, `ingest`, `flows` | `chart/templates/dapr-statestore.yaml:62`; `chart/values.yaml:964-1011` |
| `lineage-pubsub` | `pubsub.jetstream` (publish-only) | catalog, + maintenance under `lineageEmit` | `chart/templates/dapr-component.yaml:8-23` |
| `lineage-pubsub-<appId>` ×6 | `pubsub.jetstream` (subscribe) | one per subscriber, `queueGroupName: <appId>` | `chart/templates/dapr-component.yaml:109-152`; `_helpers.tpl:516` |
| `catalog-control-pubsub` | `pubsub.jetstream`, no queue group | catalog replicas (broadcast ring buffer) | `chart/templates/dapr-component.yaml:26-31` |
| `catalog-control-pubsub-lance-ray` | `pubsub.jetstream`, durable + queue group | the cascade head | `chart/templates/dapr-component.yaml:68-84` |
| `maintenance-cron` | `bindings.cron`, `@every 120s` | `maintenance` | `chart/templates/maintenance.yaml:9-20`; `chart/values.yaml:821` |
| `maintenance-reconcile-cron` | `bindings.cron`, `@every 300s` | `maintenance` | `chart/templates/maintenance.yaml:26-37`; `chart/values.yaml:826` |
| `lineage-reconcile-cron` | `bindings.cron` | `lineage` | `chart/templates/services.yaml:178-188` |

**Subscriptions are programmatic everywhere** — each app serves `GET /dapr/subscribe` via
`dapr-ext-fastapi`, and the chart holds zero `Subscription` CRDs and says so
(`chart/templates/dapr-component.yaml:3`). Verified across all five subscriber apps
(`lineage/api/dapr.py:70`, `medallion/api/events.py:36`, `medallion/api/bronze_arrival.py:37`
and `:65`, `medallion/api/train.py:101`, `medallion/api/dlq.py:30`, `catalog/api/dapr.py:84`).

**Secret-store scoping — resolved.** The first draft asked whether `lance-secrets` really derives
its scopes from `stateStore.scopes`, given that `lineage`, `medallion` and `maintenance` all
fetch secrets and are absent from that list. The verification read the template:
`dapr-component.yaml:219` is `{{- $secretScopes = concat $secretScopes .Values.stateStore.scopes }}`
under a comment "Derived from stateStore.scopes rather than restated" — a **concat onto
hardcoded branches**, not a replacement. Both readings of the first draft were half-right; there
is no anomaly here.

### 1.3 Two live anomalies, in priority order

The first draft listed three. The third — "the cascade head talks HTTP to a gRPC client" — was
**refuted as a cascade defect** and demoted to §2.1 as dead-code hygiene.

1. **`lance-statestore` cannot hot-reload, and this is upstream behaviour by design.** Reproduced
   live 2026-08-07, verbatim and every 60 s without a gap from 07:54:47Z to 08:18:47Z on
   `rask-catalog-897b5dc9c-8x4dl` (`KUBECONFIG=/etc/rancher/k3s/k3s.yaml`):
   `level=error msg="Aborting to hot-reload a state store component that is used as an actor state store: lance-statestore (state.postgresql/v1)" app_id=catalog scope=dapr.runtime.hotreload.reconciler ver=1.18.1`.
   Trigger is the chart's own `actorStateStore: "true"` (`chart/templates/dapr-statestore.yaml:62`).
   Any state-store change needs a rollout of every scoped app; `chart/values.yaml:996-999` already
   says so. See §2.21 — the residual defect is the error-per-minute-forever presentation, not the
   refusal. **Nothing to file upstream.**
2. **The medallion producer answers to app-id `lance-ray`** (`chart/values.yaml:765`) — named for
   the branch it arrived on, not for what it is. It appears in the component scopes
   (`chart/templates/dapr-component.yaml:68-84`), in the DLQ topic (`dlq.lance-ray`,
   `chart/templates/medallion.yaml:140`), in the resiliency `$subscribers` list which drives both
   `targets.components` and the CRD's own `scopes:`, and in every operator's grep.

### 1.4 Correct as shipped — do not re-litigate

Recorded so nobody spends a day rediscovering it:

- **The `@actormethod` wire-name trap is properly solved and gated.** `TypedActorProxy`
  dispatches `__actormethod__`, not the Python name (`services/annotator/src/annotator/projects/proxies.py:32`),
  which is required because the SDK keys its dispatch map on that attribute
  (`.venv/…/dapr/actor/runtime/_type_utils.py:54-56`) and `ActorProxy.__getattr__` raises
  `AttributeError` otherwise (`.venv/…/dapr/actor/client/proxy.py:207-212`). A sweep test
  bans raw `ActorProxy.create` outside `proxies.py`
  (`tests/unit/test_actor_proxy_names.py:78`). (The one residual hole in that gate is §3's
  null-wire-name item — the mechanism here is right.)
- **Actor reentrancy is not enabled and no call cycle exists.** The one hazard — the publish
  saga calling back into the project actor — is avoided by scheduling it outside the turn
  (`project_actor.py:433`, with the deadlock reasoning in the docstring). The only in-turn
  cross-actor call is task → project (`actor.py:237-248`), and neither project nor tenant
  actor opens a proxy inside a turn.
- **Absent vs unreadable is handled properly in the state plane.** `UserStateUnreadable`
  exists precisely so an unreadable record is never reported as absent
  (`packages/service-kit/src/service_kit/governed/user_state.py:85-91`); `_call` maps every
  transport failure to a fail-closed `ServiceUnavailableError` (`:262-282`); the endpoint layer
  turns it into a 409 that says "It has NOT been changed"
  (`services/catalog/src/catalog/api/v1/endpoints/user_state.py:139-156`). The actor path gets
  the same property free: `dapr/clients/http/client.py:99-102` raises on non-2xx, so
  `try_load_state` returns `(False, None)` only for a genuine miss. **This is the standard §2.17
  and §3's viewer item fail to meet in the secret plane.**
- **The lakehouse secret path is fail-closed end to end**, through one shared helper
  (`packages/service-kit/src/service_kit/governed/secrets.py:80-91`), with the chart omitting
  the plaintext value under `{{- if not (include "lance.secretsViaDapr" .) }}`
  (`services.yaml:93-100`, `:256-264`, `medallion.yaml:180-187`, `maintenance.yaml:119-125`).
  No consumer reads env after a store miss. **One credential is outside this guard — §2.9.**
- **Per-app pub/sub scoping and the queue-group split are right.** `lineage` and `lance-ray`
  both consume `lineage.events.v1`; one shared queue group would split those messages instead
  of fanning out (`_helpers.tpl:512-514`, verified against `lineage/api/dapr.py:72` and
  `medallion/api/bronze_arrival.py:39`).
- **No declared publisher lacks a consumer.** Every topic traced in both directions:
  `lineage.events.v1`, `catalog.control.v1`, `medallion.bronze`, `medallion.silver`,
  `medallion.media`, `training.jobs`, `dlq.*`.
- **The medallion's own `DaprClient` is constructed correctly.** `app.state.dapr = DaprClient()`
  with **no address**, from `dapr.aio.clients` (`services/medallion/src/medallion/producer.py:67`,
  `mover.py:53`), resolved by `api/dependencies.py:19`. A bare constructor takes the SDK default
  `f'{DAPR_RUNTIME_HOST}:{DAPR_GRPC_PORT}'` (`.venv/…/dapr/clients/grpc/client.py:162-165`),
  i.e. 50001. This is what refutes the first draft's §2.1 blast-radius claim.
- **`flows`'s workflow body is genuinely replay-safe**, checked clause by clause against
  DWF-DET-001..015 (`services/flows/src/flows/workflow.py:55`): `topo_waves` is pure and sorts
  every wave (`graph.py:98`, `:109`), the run id is `ctx.instance_id`, `when_all([])` is
  deliberately avoided (`:76-79`), no clock anywhere.
- **`flows.run_node` returns a failed `NodeResult` instead of raising**
  (`services/flows/src/flows/activities.py:41`), so `NODE_RETRY` cannot burn on a
  business-level refusal. This is the shape every new activity should copy.
- **`flows`'s workflow return annotation is right and ingest's is not.**
  `flows/workflow.py:46` is `-> Generator[Any, Any, dict[str, Any]]` with the reasoning at
  `:50-51`. See §3.

---

## 2. Defects that reproduce

Ordered by what to fix first. Severity is about blast radius, not about how hard the fix is.
Every entry below carries a verdict from the adversarial pass.

### 2.3 No error boundary in `ingest_run` — one failing chunk kills the run before `finalize`

**CONFIRMED · severity high. The highest-blast-radius item in this file.**

`services/ingest/src/ingest/workflow.py:257`: `fanout = wf.when_all([ctx.call_child_workflow(chunk_run, input=c) for c in chunks])` — **no `retry_policy=`** — awaited bare at
`results = yield fanout` (`:291`). There is **no `try`/`except` anywhere** between `def ingest_run`
(`:170`) and `return outcome` (`:307`), nor in `chunk_run` (`:310-342`); both bodies were read
line by line.

The propagation path was verified in the vendored runtime, not inferred:
`WhenAllTask.on_child_completed` completes on the **first** failed child —
`if task.is_failed and self._exception is None: self._exception = task.get_exception(); self._is_complete = True`
(`.venv/…/dapr/ext/workflow/_durabletask/task.py:424-432`) — and the worker throws it into the
orchestrator generator: `next_task = self._generator.throw(self._previous_task.get_exception())`
(`worker.py:1222`). The `MAX_RUN_HOURS` branch is not an escape either: `results = fanout.get_result()`
(`:289`) re-raises via `elif self._exception is not None: raise self._exception` (`task.py:350-351`).

So one `chunk_run` whose `drain_chunk` exhausts `ACTIVITY_RETRY` (4 attempts, ~35 s of backoff —
a NATS blip outlives it) raises straight out of the parent, and all three things the terminal
path exists to guarantee are lost, structurally:

- `finalize` (`:300`) never runs → staged fragments are never committed and `purge_staged` never runs;
- `emit_terminal` (`:306`) never runs → no FAIL reaches the graph, and the START emitted at accept
  is orphaned forever;
- `release_run_units` (`:493`, inside `emit_terminal`) never runs → the run's units sit on the
  WORK_QUEUE stream with no consumer, the exact strand `queue.release_run` was written to close
  (`queue.py:249-266`, "the live estate sat at messages: 1, consumers: 0 for hours").

The precedent is documented in-tree: `services/ingest/tests/test_empty_commit.py:9-12` describes
this chain **verbatim** ("burns its four ACTIVITY_RETRY attempts… kills the workflow BEFORE
`emit_terminal` — the run's own FAIL never reaches the lineage graph and the START emitted at
accept is orphaned forever"), and the fix that shipped was the single-input guard at
`runtime.py:345` (`if not all_fragments:`).

**Fix.** Wrap fan-out + finalize, convert the exception into a FAILED `RunOutcome` carrying the
reason, and route every exit through one terminal step so `emit_terminal` is structurally
unskippable. Same fix as the empty-commit guard, applied where it generalizes.

### 2.5 `finalize` is not idempotent — a replay after a landed commit re-appends

**CONFIRMED · severity high.**

`services/ingest/src/ingest/runtime.py:397`. The commit is
`version, tier_rows = catalog.commit(spec.namespace, spec.dataset, all_fragments, read_version=catalog.describe_version(...), run_id=spec.run_id)`
(`:397-403`) — `read_version` **re-read per attempt** — and `purge_staged(uri, spec.run_id)`
follows only at `:415`, with its own comment explaining why it must come after ("Purging earlier
would delete the record a retried finalize needs").

**The run id never reaches the wire.** `catalog_service.py:217-222` posts
`payload = {"fragments": [json.loads(f) for f in fragments_json], "read_version": read_version}`;
`run_id` is used solely at `self.registered.append((self.table_id(...), version, run_id))`
(`:239`), an in-process list. The receiving end takes no key either:
`def commit_appended_fragments(location, so, fragments, read_version)`
(`services/catalog/src/catalog/services/dataplane.py:599`) — no `run_id` parameter, and the body
is a plain Append.

So: commit lands → pod dies before the `TaskCompleted` is durable → Dapr re-executes `finalize` →
`discover_staged` still returns the same fragments (purge never ran) → **a second Append of the
same rows**, and it will not even 409, because `read_version` is re-read to the version the first
commit produced.

The supporting claim holds and is **worse than the first draft stated**: `register_version`
(`lander.py:88-94`, "how a died-after-commit run is reconciled from storage truth") is called only
on the Lander branch (`lander.py:124`), which the deployed path **bypasses entirely** —
`RASK_INGEST_USE_CATALOG: "true"` (`chart/values.yaml:166`) takes `if hasattr(catalog, "commit")`
at `runtime.py:393` — and the only implementation, `ingest/catalog.py:88-90`, appends to an
in-memory list. Nothing writes the run id to durable commit metadata, so nothing could read it back.

**Fix.** Send `run_id` as an idempotency key on the commit body and return the existing version on
a repeat, or write it into Lance commit metadata and read it before appending. A dedupe against
the last committed version is the minimum.

### 2.4 The deadline path abandons its children, then purges the queue underneath them

**CONFIRMED · severity medium** (medium only because reaching it requires a >24 h run; the
mechanism is not in doubt).

`services/ingest/src/ingest/workflow.py:277`: `winner = yield wf.when_any([fanout, deadline])`;
`if winner is deadline:` → emit FAILED and `return timed_out` (`:278-288`).

`when_any` does **not** cancel the loser — `WhenAnyTask.on_child_completed` only records the first
completer (`if not self.is_complete: self._is_complete = True; self._result = task`,
`_durabletask/task.py:573-580`) — and the only `cancel` in the SDK is `CompletableTask.cancel`
(`task.py:466-479`), a client-side mutation. Nothing terminates the child instances:
`grep -rn 'terminate_workflow|purge_workflow' --include=*.py services/` returns **nothing**.

Then `emit_terminal` runs `_run_async(release_run_units(spec.run_id))` (`:493`) →
`queue.release_run` → `await self._js.purge_stream(STREAM, subject=unit_subject(run_id))`
(`queue.py:273`) and `await self._js.delete_consumer(STREAM, f"ingest-{run_id}"…)` (`queue.py:282`)
— **precisely the durable that live `drain_chunk` activities are pulling from**
(`pull_subscribe(…, durable=f"ingest-{run_id}"…)`, `queue.py:239-247`). `purge_staged` is called
only at `runtime.py:370` and `:415`, both inside `finalize_run`, which the deadline branch never
reaches — so fragments staged by the orphans are never committed and never collected.

Live: `RASK_INGEST_MAX_RUN_HOURS: "24"` (`chart/values.yaml:169`), read at module scope
(`workflow.py:67`).

**UNVERIFIED:** whether an orphaned drain in this state fails loudly (ack against a deleted
consumer) or silently keeps staging. That needs a run, not a read.

**Fix.** Pass explicit deterministic child instance ids (`instance_id=f"{run_id}-c{n}"` — better
for operability regardless), terminate them recursively via an activity on the deadline branch,
and only then release the queue. If terminating children is not wanted, at minimum move
`release_run_units` off the deadline path so it cannot race live drains.

### 2.13 `enumerate_chunks` returns the run's entire key set as one activity result

**PARTLY · severity medium. Structure CONFIRMED, magnitude UNVERIFIABLE from source.**

The structure is exactly as described: `keys = list(iter_unit_keys(build_source(source_spec)))`
(`workflow.py:389`) materializes the whole set, the loop at `:393-408` builds one
`ChunkSpec(... keys=window ...)` per 1000 keys (`CHUNK_SIZE = 1000`, `:57`), and the entire list
is returned (`:409`) as **one** activity output awaited at `:190-194`. The docstring tension is
real and quotable — `:21-25` promises "Persisting and replaying a million activity results would
melt the state store… one child workflow per ~1-10k keys returns ONE compact result" while
`:370-381` restates it for the drain side only. **Worth adding:** the keys are then carried a
**second** time as each child workflow's input (`:257`), so history holds them at least twice.

**What did not survive is the consequence.** The 120 MB figure is the doc's own arithmetic, and
nothing in the code or the SDK establishes a failure mode: `DaprGrpcClient.__init__` sets
`grpc.max_receive_message_length` from `max_grpc_message_length` or
`settings.DAPR_GRPC_MAX_INBOUND_MESSAGE_SIZE_BYTES` (`grpc/client.py:151-160`), **neither of which
this repo sets**, and no state-store row limit is asserted anywhere.

**Fix.** Measure first. Then: write the key set into the run's staging prefix (it already exists
per run — `staging.staging_root`, `staging.py:68-70`) and return chunk **pointers**
(`{run_id, chunk_id, offset, count, …}`); `publish_chunk_units` reads its own slice. History then
carries O(chunks), which is what the docstring already claims.

### 2.2 The "orchestrator IS the outbox" docstring overstates delivery

**CONFIRMED · severity low.** (The larger claim this entry originally carried — that the
swallowing `_emit` is a defect to be fixed by letting it raise — was ruled BY-DESIGN. See §2.22.)

`services/ingest/tests/test_lineage_output_facets.py:179`. The docstring says "the runtime
checkpoints activity completion… so the workflow cannot advance past the terminal emit without it
having happened. The orchestrator IS the outbox" (`:184-188`). The assertion under it is a
signature check and nothing else:
`first = next(iter(inspect.signature(emit_terminal).parameters.values()))` /
`assert first.annotation in (WorkflowActivityContext, "WorkflowActivityContext")` (`:198-201`).

Because `LineageRecorder._emit` swallows (`lineage.py:269-275`, deliberately — I8), **"the
activity completed" and "the event was delivered" are different facts**, and the activity returns
successfully for both. The checkpoint attests to the former only.

The re-delivery half of the claim **is** true: `lineage_run_id` is `run_id_for(f"ingest:{run_id}")`,
a deterministic uuid5 (`lineage.py:126-130`), so a replayed emit rewrites one run.

**Fix.** Correct the two docstrings to say what the checkpoint actually attests. **Do not** change
the code to match them — that would revert I8; see §2.22.

### 2.1 `service_kit`'s `DaprClient` seam is aimed at the sidecar's HTTP port

**CONFIRMED (the port) · severity low. Dead-code hygiene, not a cascade defect.**

`packages/service-kit/src/service_kit/__init__.py:63` is
`return dapr_client_cls(f"http://127.0.0.1:{settings.dapr_http_port}")` with
`dapr_http_port: str = Field(default="3500", alias="DAPR_HTTP_PORT")` (`config.py:42`), and
`_import_dapr_client` returns `from dapr.clients import DaprClient` — `class DaprClient(DaprGrpcClient)`
(`.venv/…/dapr/clients/__init__.py:51`), whose param is documented
`address (str, optional): Dapr Runtime gRPC endpoint address` (`:78`). gRPC is 50001.

**The scheme is harmless; the port is the whole defect.** Traced through the parser rather than
assumed: `GrpcEndpoint("http://127.0.0.1:3500")` accepts the scheme but rewrites it —
`if self._parsed_url.scheme in ['http','https']: self._scheme = URIParseConfig.DEFAULT_SCHEME`
with a deprecation `warn` (`dapr/conf/helpers.py:39-45`; `ACCEPTED_SCHEMES` at `:10` includes
http/https) — yielding endpoint `dns:127.0.0.1:3500`, tls False, hence
`grpc.insecure_channel('dns:127.0.0.1:3500')` (`grpc/client.py:179-182`). A gRPC channel aimed at
daprd's HTTP/1.1 listener.

**It is pinned by a test** — `assert captured['address'] == 'http://127.0.0.1:3500'`
(`packages/service-kit/tests/test_dapr.py:45`) — so the wrong value is currently a *specification*.
That is why this stays CONFIRMED rather than being dismissed as cosmetic.

**The seam has no consumer.** `grep -rn "state\.dapr|get_dapr|DaprClientDep" --include=*.py services/ingest services/flows services/controlplane services/compute services/gateway`
returns **nothing**. `make_service_app` builds the client at `__init__.py:112` and closes it at
`:118`; nobody makes an RPC on it, and gRPC channels are lazy, so a wrong port raises nothing at
boot. The gateway — the other candidate — composes the HTTP invoke URL itself
(`gateway/__init__.py:216-217`: `f"http://127.0.0.1:{port}/v1.0/invoke/{app_id}/method"` with
`DAPR_HTTP_PORT`, the **correct** port for that API).

**Fix.** Correct the constant to the gRPC port at the single seam and fix the test that pins it.
Better: delete the seam if the next reader also finds no consumer.

### 2.6 The task actor disarms its lease reminder **before** persisting the transition

**PARTLY · severity medium. Ordering CONFIRMED; the "unrecoverable without operator surgery"
consequence is overstated.**

`services/annotator/src/annotator/projects/actor.py:200`. In `fire`,
`await self._disarm_lease()` runs at `:192` (assign), `:200` (submit) and `:203`
(release/lease_expired/skip), and `await self._store(task)` only at `:221` — where `_store` is
`set_state` + `save_state` (`:98-100`), a real store round-trip that can raise. The sibling actor
gets it the other way round and says why: `project_actor.py:252-255` — "Arm the watchdog BEFORE
persisting the transition… The reverse order could persist `publishing` with no watchdog, the
exact stranding this reminder exists to prevent."

**The narrowing.** Two live edges out of CLAIMED remain fireable by a principal:
`(TaskState.CLAIMED, "release"): (TaskState.UNASSIGNED, "can_annotate")` and
`(TaskState.CLAIMED, "skip"): (TaskState.SKIPPED, "can_annotate")` (`machines.py:56`, `:58`), and
`release` is documented at `machines.py:78-81` as "the documented escape hatch for a task pinned
to someone unavailable… the API layer allows the holder, or anyone holding `can_manage`". A
manager un-sticks it through the normal API. Further, a raising `_store` propagates out of `fire`,
so the caller gets an error and can retry the same event, re-running disarm + store. **What is
actually lost is the automatic safety net** — the lease stops self-expiring — until a human acts.

**Fix.** Move both `_disarm_lease()` calls after `await self._store(task)`. The reorder costs
nothing: `_disarm_lease` already swallows a failed unregister (`:319-323`) and a surviving
reminder is already handled (`:332-334`).

### 2.7 `POST /v1/stores` read-modify-writes a **shared** document with no etag

**CONFIRMED · severity medium** (narrow window — two admins attaching at once).

`services/catalog/src/catalog/api/v1/endpoints/stores.py:102`. Full read-modify-write with no
token: `existing = await _attached(state)` (`:102`) then
`await state.put(subject=ESTATE_SUBJECT, document=UserStateDocument.ATTACHED_STORES, value=_ATTACHED.dump_python([*existing, attached], mode="json"))`
(`:108-112`).

The client offers no concurrency control: `UserStateStore.put`
(`packages/service-kit/src/service_kit/governed/user_state.py:230-237`) documents "Last write
wins, deliberately: this is one person's own work… There is no etag round trip because there is
no second writer to lose a race with", and `_call` sends
`json=[{"key": key, "value": stored.model_dump(mode="json")}]` (`:240-246`) with no `etag` and no
`options.concurrency`. **The premise is false for this document specifically:** `ATTACHED_STORES`
is "ESTATE-scoped, not per-user: it is written under the reserved `ESTATE_SUBJECT`"
(`user_state.py:167-172`), so every estate admin writes the same key. Two concurrent attaches
drop one silently.

**Fix.** Return the sidecar's `ETag` from `get`, accept an optional `etag` on `put`, emit
`options.concurrency: first-write`, map 409 to a domain `ConflictError`, and retry the
read-modify-write once in `attach_store`. Keep last-write-wins for the per-subject documents —
the docstring's reasoning is right for those.

### 2.8 The catalog's privileged-identity door can never open

**CONFIRMED · severity medium** (latent — the chart never sets the subject list).

`services/catalog/src/catalog/api/security.py:91-96` passes exactly four kwargs — `token=`,
`identity=`, `allowed_subjects=`, `privileged_subjects=` — and **no `dedicated_token=`**. The
shared helper defaults it to `None` (`dapr_auth.py:149`) and then:
`dedicated = dedicated_token(identity) if dedicated_token else None` /
`if not dedicated: raise HTTPException(status_code=401, detail=f"service identity {identity!r} is privileged but has no dedicated credential provisioned")`
(`dapr_auth.py:180-182`). `HTTPException` is **not** `ServiceDoorClosed`, so it is not caught by
the `except ServiceDoorClosed: pass` fall-through at `security.py:97-100` — a privileged subject
is hard-refused and never reaches OIDC.

Latency confirmed: `privileged_subjects: str = Field(default="", alias="LANCE_PRIVILEGED_SUBJECTS")`
(`catalog/core/config.py:209`) and `grep -rni privileged chart/` returns only CNPG CRD noise and
an unrelated `_helpers.tpl` securityContext line.

**Correction to the first draft:** lineage does **not** "supply the callback". It has its own
forked `_service_principal` (`services/lineage/src/lineage/api/security.py:90`) that calls
`_dedicated_token(identity)` inline at `:122` and never calls the shared
`dapr_auth.service_principal` at all. So the extraction the `dapr_auth.py:153-155` docstring
claims is half-done in **both** directions — lineage still carries the original.

**Fix.** Lift lineage's `_dedicated_token` into `service_kit.governed.dapr_auth`, pass it from the
catalog, and delete lineage's fork so there is one door.

### 2.9 `MEDIA_PUBLISH_CLIENT_SECRET` rides plaintext pod env

**CONFIRMED · severity medium.**

`chart/templates/explorer.yaml:167` —
`- { name: MEDIA_PUBLISH_CLIENT_SECRET, value: {{ $root.Values.dex.clientSecret | quote }} }` —
inside the plain `{{- if eq $name "annotator" }}` block with **no**
`{{- if not (include "lance.secretsViaDapr" ...) }}` wrapper, unlike every other credential
(`services.yaml:93`, `:256`, `:311`; `medallion.yaml:180`, `:310`; `maintenance.yaml:119`). The
contradicting comment is five lines above at `:162-164`: "Deliberately NO password here — it is
seeded into OpenBao and fetched via the Dapr secret store, fail-closed (the estate's secrets
rule)".

The store genuinely has nothing to serve: `openbao.yaml:157-159` seeds `rustfs-secret-key`,
`postgres-password`, `publisher-oidc-password`, `dapr-state-connection-string` — no client secret.
The binding is env-only: `publish_client_secret: str | None = Field(default=None, alias="MEDIA_PUBLISH_CLIENT_SECRET")`
(`packages/service-kit/src/service_kit/media/config.py:79`). Both land in the same call:
`lakehouse.py:91` `bundle = fetch_required_secrets(..., require="publisher-oidc-password")` and
`:98` `auth = (settings.publish_client_id, settings.publish_client_secret or "")` → one
`httpx.post` at `:99`.

**Fix is free** — one extra field off a bundle already fetched. Seed it into `secret/lance`
alongside `publisher-oidc-password`, read it from the same bundle, drop the env row behind the
standard guard.

### 2.10 The lineage DLQ route replays the whole 168 h backlog on every pod restart

**CONFIRMED · severity medium** (bounded to a week's parked backlog).

`chart/templates/dapr-component.yaml:123` is
`- { name: deliverPolicy, value: {{ .deliverPolicy | quote }} }`; `:124` is
`{{- if eq .deliverPolicy "new" }}` gating the `durableName` at `:128`. The subscriber list at
`:102` seeds `dict "appId" .Values.services.lineage.daprAppId "deliverPolicy" "all"`, so lineage's
component gets `deliverPolicy=all` and **no `durableName`** — an ephemeral queue-group consumer
that starts from the beginning of the stream on every attach. The chart says the ephemerality is
intentional (`:99-101`, "lineage stays ephemeral ON PURPOSE — a durable cursor would defeat its
replay-rebuilds-the-graph recovery story").

**The DLQ rides that same component:**
`dapr_app.subscribe(pubsub=settings.dapr_pubsub, topic=settings.dapr_dlq_topic, route="/lineage-dlq")(on_dead_letter)`
(`services/lineage/src/lineage/api/dapr.py:76`) — same `settings.dapr_pubsub` as the main
subscription at `:69`. Retention: `nats-stream-job.yaml:103` `add_if_missing DLQ "dlq.>"` through
the `limits`-retention builder with `--max-age=168h` (`:58`). Cost per replayed message:
`log.error("dapr_dead_letter_parked", ...)` + `record_outcome(Outcome.DEAD_LETTERED)`
(`dapr.py:47-54`) — the exact series the dashboard graphs. The handler's own docstring names the
mismatch: "No auto-requeue… the DLQ adds operator VISIBILITY, not a second path", while the
component gives it replay semantics.

Every rollout spikes the terminal-loss metric with no new loss, which trains operators to ignore
the one panel that means provenance is missing.

**Fix.** Give the DLQ its own component with `deliverPolicy: new` plus a `lineage-dlq-durable`
cursor.

### 2.11 The mover DLQ topic is per-subTopic, so two movers park and count each other

**CONFIRMED · severity medium** (metric double-count and non-attribution, not data loss).

`chart/templates/medallion.yaml:293` renders
`- { name: MEDALLION_DLQ_TOPIC, value: "dlq.{{ .subTopic }}" }` under a comment at `:291-292`
claiming "retry exhaustion parks the trigger on this per-app topic". Two movers share the
subTopic: `chart/values.yaml:794` (`bronze-to-silver … subTopic: medallion.bronze`) and
`:811` (`pages-to-gold-htr … subTopic: medallion.bronze`). Both render `dlq.medallion.bronze`,
both declare it as `dead_letter_topic`, and both subscribe to it (`events.py:34` → `dlq.py:30`
`@dapr_app.subscribe(pubsub=pubsub, topic=dlq_topic, route="/dlq-event")`).

Their pubsub components differ (`lance-pubsub-<appId>`, distinct `queueGroupName` and distinct
`<appId>-durable`), **so this is a fan-out, not a competing group**: every dead letter from either
app is delivered to both. And the label is a literal:
`register_dlq_route(dapr_app, ..., app_label="mover")` (`events.py:34`) → `record_dead_letter("mover")`
for both. The producer's correct counterexample is `medallion.yaml:140`, `value: "dlq.lance-ray"`.

**Fix.** Render `dlq.{{ .daprAppId }}` and pass the mover's app-id as `app_label`.

### 2.12 `bronze_arrival` names a de-duplication mechanism that does not exist

**CONFIRMED · severity medium.**

`services/medallion/src/medallion/api/bronze_arrival.py:59-61` justifies two live cascade heads:
"Both heads publish the same `medallion.bronze` trigger, and the movers' own token
de-duplication is what keeps a table that emits BOTH signals from cascading twice." **No such
mechanism exists in `transform.py`.** The only concurrency construct is
`_write_lock = asyncio.Lock()` (`:63`), whose own comment (`:56-62`) says what it is and is not:
"Single-flight guard for the stage WRITE… preventing two `write_dataset(mode=\"overwrite\")`… the
write stays overwrite-idempotent so scaling replicas is still safe (last-writer-wins on identical
deterministic content), the lock just removes the concurrent commit contention." `token` is only
ever read (`token = data.get("token")`, `:94`) and threaded into logs and lineage run-ids — never
compared against a seen-set.

There is nowhere to keep one either: `chart/values.yaml:964-1011` `stateStore.scopes` is exactly
`annotator, catalog, ingest, flows` — **no mover app-id**.

**The two heads mint incompatible tokens.** `ingest_trigger.py:112` is `token = _cascade_token(data)`,
defined at `:62-76` as the `run.facets.lance.token` field falling back to `run.runId`;
`publication_trigger.py:103` is `"token": str(data.get("event_id") or "")`. A token-keyed dedup
could not match them. The lane-discrimination guard at `transform.py:110-113` keys on `dataset`,
not `token`, and both heads carry the same `dataset` — so it does not incidentally cover the
double-fire either.

**Fix.** Pick one and make it true. Either implement it (which requires scoping the movers to
`lance-statestore` and a single shared token derivation), or delete the claim and state plainly
that a doubly-triggered table cascades twice — accepted because the stage write is
overwrite-convergent, at the cost of duplicate lineage Runs per hop. A safety property that
exists only in a comment is the worst of the three. **See §4 of `open_ingest_design.md`:** the
recommendation there is to retire one of the two heads entirely, which dissolves this.

### 2.19 The sweep has no rotation and no cursor

**CONFIRMED · severity medium.**

`services/maintenance/src/maintenance/services/sweep.py:179` is `for uri in uris:`, over the list
built at `:140-156` by appending `discover_datasets(fs, bucket).uris` bucket by bucket.
`discover_datasets` (`optimize.py:68-101`) is a depth-first
`fs.get_file_info(pafs.FileSelector(prefix, recursive=False))` walk — object-store listing order,
deterministic across ticks. **No shuffle, no offset, no persisted cursor anywhere in the
function.** The only skip is `_policy_skip_reason` (`:193` → `:54-90`), whose interval branch is
gated on `interval = policy.get("compact_interval_hours")` and only stamps for datasets whose
resolved policy sets it (`:236-239`) — with no policy records the whole estate is re-swept from
the top every tick. A pass that consistently dies at dataset N never maintains anything after N,
silently, forever.

**The module makes exactly this argument against itself 140 lines earlier** for the FAIL-emit cap
(`:38-42`, "the discovery listing order is deterministic, so a fixed head-slice would re-drop the
SAME datasets every tick and their FAIL would never emit — shuffling gets every failing dataset
through within a few ticks") and applies `random.shuffle(failed)` at `:324`. The sweep's own
iteration never got it.

**Fix.** Shuffle `uris` per tick, or persist a rotation offset. ~10 lines.

### 2.20 A lost sweep is invisible

**CONFIRMED · severity medium.**

`services/maintenance/src/maintenance/services/sweep.py:255` — `record_run()` fires only **after**
the `for uri in uris:` loop that starts at `:179`; nothing is emitted before or during it.
`compaction.runs` is the sole liveness series (`metrics.py:11-15`,
`_runs = _meter.create_counter("compaction.runs", unit="{run}", description="Compaction sweeps triggered by the Dapr cron binding.")`).
The summary is likewise only produced on the return path (`routes.py:67-69`,
`summary = summarize(results)` / `log.info("maintenance_sweep", extra=summary)` after
`await run_in_threadpool(run_sweep, settings)`). There is no per-dataset counter and no started
counter.

So a process killed at dataset 400 of 900 is observationally identical to a tick that never
arrived — **and because of §2.19 it is always the same first 400.**

**Fix.** A started counter before the loop and a `compaction.datasets.swept` counter inside it.
This is a **prerequisite for §4**, not a side issue: nobody can currently measure how often a pass
is lost, so the durability argument has no evidence behind it either way.

### 2.17 The lineage privileged-auth path does an uncached, unbounded secret fetch

**PARTLY · severity low. Code shape CONFIRMED; "per request" is overstated — the path is latent.**

`services/lineage/src/lineage/api/security.py:87` is
`return fetch_dapr_secret(store, key).get(f"service-token-{identity}") or None` — no cache, no
`retries=`, so it takes the boot defaults `timeout: float = 5.0, retries: int = 10, backoff: float = 3.0`
(`secrets.py:41-44`) with `wait_exponential_jitter(initial=backoff, max=15)` (`:60`). **UNVERIFIED
arithmetic** (checks out from those parameters): 9 sleeps of 3, 6, 12, 15×6 ≈ 111 s, plus up to
10×5 s of connect timeouts. It is called from `_service_principal` (`:122`), reached from
`def authenticate(...)` (`:134`) — a **sync** FastAPI dependency, so the AnyIO-threadpool exposure
is real. The estate already knows the shape: the viewer uses `@lru_cache(maxsize=32)`
(`objects.py:70`) and `fetch_dapr_secret(store, secret, retries=1)` (`:86`).

**The narrowing.** The fetch is inside `if identity in privileged:` (`:118`), and
`privileged_subjects: str = Field(default="", alias="LINEAGE_PRIVILEGED_SUBJECTS")`
(`lineage/core/config.py:82`) is never set by the chart. Like §2.8 this is **latent today**, not a
live 40-token stall. Also: `_is_transient` (`secrets.py:28-35`) fails a 4xx immediately, so the
~160 s only applies to connect errors and 5xx.

**The secondary claim is fully CONFIRMED.** `except Exception … return {}` (`secrets.py:71-73`)
means exhaustion and a genuinely absent field both arrive as falsy, and both produce the identical
`UnauthenticatedError(f"service identity {identity!r} is privileged but has no dedicated credential provisioned")`
at `:123` — **the absent-vs-unreadable conflation the estate solved properly in the state plane**
(§1.4).

**Fix.** `@lru_cache` + `retries=1`, and raise a 503 when the store could not be **read**,
returning `None` only when the bundle was read and the field was genuinely absent.

### 2.18 The maintenance step order is documented backwards, in three places

**CONFIRMED · severity low. Documentation-only, but it invites reordering working code.**

`services/maintenance/src/maintenance/services/optimize.py:120` states "The order is compact →
cleanup → optimize indices, and it is FIXED, not configurable: compaction obsoletes files, so
cleanup must follow it, and index optimization must follow that." The code runs
`compact_files(...)` at `:177` (fallback `:188`) → `ds.optimize.optimize_indices()` at `:202` →
`ds.cleanup_old_versions(...)` at `:248` (in the `elif cleanup_enabled:` branch). The same wrong
order is restated at `service.py:6-8` and paraphrased at `routes.py:9`. **An inline comment inside
the same function already describes the CODE order correctly** (`:196-198`, "Skipping index
optimization after a compaction leaves the new fragments unindexed"), so the file contradicts
itself.

**Fix.** Correct all three, and say why cleanup is last: it reclaims the versions that *both*
compaction and index optimization superseded, in one pass.

### 2.21 `lance-statestore` cannot hot-reload — and that is upstream, by design

**CONFIRMED · severity low. The first draft's open question is now answered.**

Reproduced live 2026-08-07 (`KUBECONFIG=/etc/rancher/k3s/k3s.yaml`,
`kubectl logs rask-catalog-897b5dc9c-8x4dl -c daprd`, pod age 48 m), verbatim and every 60 s from
07:54:47Z to 08:18:47Z without a gap:

```
level=error msg="Aborting to hot-reload a state store component that is used as an actor state store: lance-statestore (state.postgresql/v1)" app_id=catalog scope=dapr.runtime.hotreload.reconciler ver=1.18.1
```

The trigger is the chart's own `- { name: actorStateStore, value: "true" }`
(`chart/templates/dapr-statestore.yaml:62`). daprd refuses hot-reload for **any** actor state
store, so the operational consequence the first draft drew is correct: a change to this component
needs a rollout of every scoped app (`annotator`, `catalog`, `ingest`, `flows` —
`chart/values.yaml:964-1011`), and `values.yaml:996-999` already says so ("a sidecar loads
components at BOOT, and M7's hot-reload abort … means a LATER addition would not reach
already-running pods without a rollout").

**What remains a defect is only the presentation:** an error-level line every 60 s forever for a
permanent, intended condition. **Nothing to file upstream.**

**Fix.** Nothing in the chart. Filter or annotate it in the log pipeline so it stops training
operators to ignore `daprd` errors on `catalog`.

### 2.22 Claimed and did not survive

These two are recorded so the same claim is not re-filed. Both were in the first draft's §2.

- **"The cascade head talks HTTP to a gRPC client" (blast-radius half) — REFUTED.**
  `bronze_arrival.py:45`'s `dapr: DaprClientDep` resolves through `medallion/api/dependencies.py:17-22`
  (`def get_dapr(request): return request.app.state.dapr`, typed `from dapr.aio.clients import DaprClient`),
  and `app.state.dapr` is set at `services/medallion/src/medallion/producer.py:67` to `DaprClient()`
  — the **async** client with **no address**, taking the SDK default port 50001
  (`grpc/client.py:162-165`). The medallion producer is not built by `make_service_app` (its own
  `@asynccontextmanager lifespan` at `producer.py:50`), so `service_kit.build_dapr_client` is never
  on that path. `grep -rn 'make_service_app'` gives exactly four composers — controlplane, compute,
  ingest, flows — and none of them uses `state.dapr`. The seam is built on every one of those pods
  and its channel is never used. **This is dead code, not the cascade head.** The port defect
  survives as §2.1 at severity low.
- **"`emit_terminal` swallows, so let the lineage emit raise" — BY-DESIGN, do not implement.**
  The swallow reproduces exactly (`lineage.py:269-275`; `runtime.py:270-275` for
  `release_run_units`) but it is the module's **stated contract**, in the module docstring:
  `lineage.py:21-24` — "**Never raises** (I8). Lineage is OBSERVATIONAL… A failed emission must
  not fail a run that actually landed its data" — restated inline at `:273-274` ("that would turn
  an observability outage into a data incident") and again at `runtime.py:265` ("tidying up must
  never fail the thing it is tidying up after"). The proposed fix would deliberately revert I8,
  and the first draft argued *past* I8 rather than against it. Two further corrections: the
  absolute claim "the activity body has no path that raises" is **false** — `terminal()` computes
  `outputs = _output_datasets(project, dataset, version, rows)` at `lineage.py:243` **outside** the
  guard (which covers only the `emit()` closure at `:245-252`), and `_output_datasets` lazy-imports
  `lineage_kit.schemas` and `ingest.naming` at `:108-110`, so an ImportError there does raise and
  `ACTIVITY_RETRY` does fire. And the real downside is narrower than stated: a graph write lost to
  a transport blip is lost for that run, recoverable only by the lineage reconcile sweep
  (`docs/LINEAGE.md`). **What survives is the docstring defect only — §2.2.**

### 2.23 Not put through the adversarial pass

Three items from the first draft's §2 were in neither verification slice. They are **neither
confirmed nor refuted** — re-verify before acting on any of them. They are held here rather than
deleted so the work is not silently lost, and rather than in §2 so they are not mistaken for
verified findings.

- **`flows` never reads workflow state** — claimed at `services/flows/src/flows/routes.py:113`:
  the durable lane stores `RunState(status="running")` into a process-local dict and `get_run`
  (`:129-134`) returns only `runs.get(run_id)`; `get_workflow_state` claimed absent under
  `services/flows`.
- **No terminate, pause, resume or purge anywhere** — claimed from a repo-wide grep, with the
  corollary that `RASK_INGEST_MAX_UNITS` is unset in the chart (deployed default `0` = unbounded)
  so a mis-pointed source has no lever but the 24 h timer, and that workflow history is never
  collected.
- **`MAX_RUN_HOURS` and `MAX_UNITS` are env reads consulted from workflow scope** —
  `workflow.py:67`, `:82`, branched on at `:218` and `:275`; claimed to diverge the action stream
  if the value changes under a rolling restart. Note that §3's `RunSpec`/`ChunkSpec` item, which
  **was** verified CONFIRMED, is the same class of hazard one field away.

---

## 3. Non-idiomatic usage worth converging

All fifteen items below were verified. None is losing data today with one exception, flagged
inline. They are the places where the next edit goes wrong.

**`POST /flows/runs` schedules a durable workflow with no auth on the route or the gateway row**
(`services/flows/src/flows/routes.py:60`). **CONFIRMED · severity high — arguably mis-filed here
rather than in §2.**
`async def create_run(request: RunRequest, http: HttpDep, settings: FlowsSettingsDep, runs: RunsDep, scheduler: SchedulerDep)`
— five dependencies, none an auth door; contrast ingest, where all three routes call
`await authorize_ingest(request, settings, ...)` (`api.py:127`, `:248`, `:289`). No router-level
defence either: `flows/__init__.py:18-20` is `make_service_app(..., routers=[health.router, routes.router], ...)`
with no `dependencies=`. The gateway row is a bare path proxy — `gateway/__init__.py:164`
`(f"{prefix}/flows", f"{prefix}/flows", *flows)` — and `_pick_route` (`:168-173`) does nothing but
longest-prefix string matching. The chart confirms the asymmetry: `governedAuth: true` occurs
**exactly once** in `chart/values.yaml`, at `:113` under ingest; the flows block (`:187-192`) has
`frontDoor: true` and no such flag. So an unauthenticated `POST /api/flows/runs` provisions a
durable workflow instance. Either add the dependency or name the boundary being relied on in
`routes.py`. Right now it is neither enforced nor named.

**`RunSpec`/`ChunkSpec` validation runs inside workflow scope and can read env**
(`workflow.py:172`, `:328`). **CONFIRMED · severity medium.** Both `spec = RunSpec.model_validate(payload)`
(`:172`) and `chunk = ChunkSpec.model_validate(payload)` (`:328`) are inside generator
(orchestrator) bodies (`def ingest_run` at `:170`, `def chunk_run` at `:310`), and both models
declare `sizing: ResolvedSizing = Field(default_factory=resolve)` (`:110`, `:140`). `resolve()`
(`sizing.py:103`) calls `default_fragment_rows/bytes/fetch_batch/fetch_concurrency` (`:53`, `:57`,
`:61`, `:65`), each an `int(os.getenv(...))` read at call time by design, and can
`raise SizingRefused(...)` (`:118`). Defended today only by `api.py:163` (always sending
`"sizing": sizing.model_dump()`) and `enumerate_chunks` (`sizing=spec.sizing,  # Carried, not re-resolved`).
**One narrowing the first draft did not make:** `resolve()` with no argument can only raise when
`RASK_INGEST_FRAGMENT_ROWS >= RASK_INGEST_MAX_ACK_PENDING`, and the shipped defaults are 1024 vs
2048 (`queue.py:64`) — so at chart defaults the default path reads env non-deterministically but
cannot raise. The env-read hazard is unconditional on the older-build payload path; the
unhandled-raise hazard needs a deployment that also raised `fragment_rows`. Give the
workflow-scope models an env-free default.

**`publish_units` idempotency is bounded by a 2-minute window the docstring does not name**
(`workflow.py:428`). **CONFIRMED · severity medium.** `workflow.py:427-434` names the mechanism
("JetStream dedupes on the message id within the stream's duplicate window") and never the bound.
The bound is `DUPLICATE_WINDOW = 120.0` (`queue.py:99`, under a comment "Matches the chart's
`--dupe-window=2m`"), and the header is set for real at `queue.py:216-220`
(`headers={"Nats-Msg-Id": _dedupe_id(task)}`). The arithmetic checks: `ACTIVITY_RETRY`
(`workflow.py:85-89`) is `first_retry_interval=5s, max_number_of_attempts=4, backoff_coefficient=2.0`
→ 5 + 10 + 20 = 35 s ≪ 120 s; a workflow **replay** does not fit. `queue.py:209-212` concedes the
rest verbatim ("Beyond that window a replay does re-queue, and the staging layer's exact-cover
selection is what absorbs it; this shrinks that surface rather than removing it"), and the named
absorber is itself unresolved: `staging.py:112-132` `StagingOverlapError` says in bold "**This is
REACHABLE.**" and closes "the remedy is a finalizer that can resolve a partial overlap, or a
batching rule that keeps redeliveries in their original grouping. Both are open." **Left
implicit in the first draft:** reaching `StagingOverlapError` needs a *partial* overlap, so a
re-publish past the window is necessary but not sufficient for the hard failure. Either raise the
window to comfortably exceed a pod restart plus reschedule (10-15 min, moved in **both**
`queue.DUPLICATE_WINDOW` and the nats-stream-job), or stop claiming construction-level
idempotency and name the bound.

**An `auto_cleanup:` failure emits no lineage event at all** (`sweep.py:311`). **CONFIRMED ·
severity medium.** The branch is exactly: `:311` `if result.error is not None:` / `:312`
`if result.error.startswith("maintain:"):` / `:313` append / `:314` `continue` / `:315`
`if not _did_material_work(result): continue` / `:317` `await emitter.emit_maintenance(...)`. The
producing site is `optimize.py:245` `result.error = f"auto_cleanup: {exc}"` — matching neither
prefix, so the `:314` `continue` swallows it **and** blocks the COMPLETE branch. The dataset can
already have done material work: `compact_files` at `optimize.py:177` sets
`result.fragments_removed` at `:189`, and the auto-cleanup block (`:227-245`) runs after it under
the comment "Applied AFTER compaction so a failure to configure can never cost us the compaction
that already succeeded". And `summarize` **does** count it: `sweep.py:358`
`"errors": {r.uri: r.error for r in results if r.error}`. The `emit_sweep_lineage` docstring
(`:280-296`) enumerates `maintain:` / `open:` / REFUSED / no-error+material / unparseable-URI and
reads as exhaustive — `auto_cleanup:` is absent from it. Decide what a dataset that compacted and
then failed to configure auto-cleanup should report; today it reports nothing while `summarize`
calls it an error.

**`attach_store` bypasses the per-document byte ceiling** (`stores.py:108`). **CONFIRMED ·
severity medium.** The ceiling lives in the *endpoint helper*, not the store client:
`catalog/api/v1/endpoints/user_state.py:167-169`
(`size = len(json.dumps(payload).encode()); if size > settings.user_state_max_bytes: raise InvalidInputError(...)`)
against `catalog/core/config.py:311` (`user_state_max_bytes: int = Field(default=512 * 1024, ...)`).
`attach_store` skips that helper entirely — `stores.py:108-112` goes straight to
`await state.put(subject=ESTATE_SUBJECT, document=UserStateDocument.ATTACHED_STORES, value=...)`,
and `UserStateStore.put` (`packages/service-kit/.../governed/user_state.py:230`) has no size check
of any kind. **So the one estate-wide, append-only document is the one write with no ceiling.**
Lift the check into `UserStateStore.put`. (Citation caveat: `user_state.py:167-169` above is the
*catalog endpoints* file; §2.7 uses the same bare filename for the *service-kit* module. Two
different files.)

**The run-deadline timer is never cancelled** (`services/ingest/src/ingest/workflow.py:276`).
**PARTLY · severity low — code half CONFIRMED, the reminder-pressure consequence is unreachable
from this repo.** `workflow.py:275-289` is
`if MAX_RUN_HOURS > 0: deadline = ctx.create_timer(...); winner = yield wf.when_any([fanout, deadline]) ... results = fanout.get_result()`
— the timer task is simply dropped on the fan-out branch. The SDK half holds:
`_durabletask/task.py:466-479` is the **only** `cancel` (`grep -n 'def cancel' task.py` → one hit)
and its body is purely local (`if self._is_complete: return; self._exception = exc; self._is_complete = True; self._parent.on_child_completed(self)`)
— no action emitted. Decisively, the SDK's action vocabulary has **no cancel factory at all**:
`grep 'def new_.*_action' internal/helpers.py` yields exactly `new_complete_workflow_action`,
`new_workflow_version_not_available_action`, `new_schedule_task_action`,
`new_create_child_workflow_action`. What could **not** be established from this repo is that the
durable timer record and its backing reminder survive — that is daprd runtime behaviour the Python
SDK never sees. With the chart's 24 h value, every successful run plausibly leaves a 24-hour timer
behind. At minimum state it in the docstring at `:268-270`, which currently reads as though the
timer is free. If reminder pressure turns out to matter, the idiomatic alternative is a bounded
`continue_as_new` loop with short timers, or a supervising activity.

**Ingest's workflows are annotated `-> dict[str, Any]` while being generators** (`workflow.py:170`,
`:310`). **CONFIRMED · severity low — measured, not inferred.**
`uvx ty check services/ingest/src/ingest/workflow.py` emits, as its first two findings,
`error[invalid-return-type]` at `workflow.py:170:70` and `workflow.py:310:69`, both "expected
`dict[str, Any]`, found `types.GeneratorType`" with "Function is inferred as returning
`types.GeneratorType` because it is a generator function". Exactly two. `flows` already ruled this
a bug and said why: `flows/workflow.py:46` is
`def flow_run_workflow(ctx, payload) -> Generator[Any, Any, dict[str, Any]]:` and `:50-51` reads
"Typed as a Generator because it IS one — every `yield` is a durable await point. Annotating it
`-> dict` (the shape it conceptually returns) is a lie the type checker catches." CLAUDE.md pins
`ty` with `error-on-warning = true`. Question already decided one directory over.

**`TypedActorProxy` computes a null wire name if any `@actormethod` is ever written without an
explicit name** (`proxies.py:36`). **CONFIRMED · severity low (latent).** Line 36 is exactly
`wire = getattr(declared, "__actormethod__", name)`, then `:37`
`return _translating(getattr(self._proxy, wire))`. The SDK sets the attribute unconditionally:
`.venv/…/dapr/actor/actor_interface.py`, `def actormethod(name: Optional[str] = None): ... def wrapper(funcobj): funcobj.__actormethod__ = name`
— so `@actormethod()` stores `None`, the three-arg `getattr` default never applies, and
`getattr(self._proxy, None)` raises `TypeError`. Latency confirmed by count: 2 declarations in
`tenant_actor.py` + 5 in `actor.py` + 11 in `project_actor.py` = **18**, every one
`@actormethod(name="...")`. The existing gate would catch neither shape:
`tests/unit/test_actor_proxy_names.py::test_python_names_reach_the_wire_names` is
`@parametrize`'d over a hardcoded list of exactly 8 `(interface, python_name, wire_name)` triples,
and `test_no_call_site_builds_a_raw_actor_proxy` only greps for the literal `ActorProxy.create`.
Two lines: make the fallback total (`… or name`) and assert every `__actormethod__` is a non-empty
`str`.

**The viewer's per-store credential lookup has a dead `except` branch**
(`services/viewer/src/viewer/api/v1/endpoints/objects.py:85`). **CONFIRMED · severity low.**
`secrets.py` wraps the entire `for attempt in Retrying(...)` block in `try:` and closes with
`except Exception as exc: log.warning("dapr_secret_fetch_failed", ...); return {}` followed by
`return {}  # unreachable; keeps the type-checker's every-path-returns view honest`. **There is no
path out of `fetch_dapr_secret` that raises.** So in `_creds` the `except Exception as exc:`
guarding `data = fetch_dapr_secret(store, secret, retries=1)` is unreachable, and control always
falls to `ak, sk = data.get("access_key"), data.get("secret_key")` →
`if not (ak and sk): raise ServiceUnavailableError(f"secret {secret!r} exists but carries no access_key/secret_key pair")`
— a claim about the secret's *contents* raised for a store that was never read. Still fail-closed
(both branches raise `ServiceUnavailableError`), so this is diagnostics, not security. Give
`fetch_dapr_secret` a `strict=True` variant that re-raises.

**The publish watchdog's spawned saga task is never referenced** (`project_actor.py:433`).
**CONFIRMED · severity low — and this is the item whose impact statement is most carefully hedged,
correctly.** `:433` is `lakehouse.spawn_publish(project.project_id)`, a bare call with the return
value discarded; `lakehouse.py:307` is `return asyncio.get_running_loop().create_task(_drive())`,
so a handle IS produced and thrown away. What is retained is only the id: `_RUNNING: set[str] = set()`
(`:277`) and `_RUNNING.add(project_id)` (`:285`). asyncio holds a weak reference. The bounding is
right: `_drive` ends `finally: _RUNNING.discard(project_id)` (`:304-305`), and coroutine
finalization on GC throws `GeneratorExit` at the await point so that `finally` does run — the guard
cannot wedge — and `PUBLISH_REMINDER` re-drives on `_PUBLISH_PERIOD = timedelta(seconds=60)`
(`project_actor.py:63-68`). Cost of a collection is a lost minute. Hold the reference anyway.

**`app.state.actors_registered` is written but never read**
(`services/annotator/src/annotator/main.py:94`, `:97`). **CONFIRMED · severity low.**
`grep -rn "actors_registered" --include="*.py" .` (excluding `.venv`) returns exactly two lines,
both writes. Zero reads. The promise is at `main.py:86-88`: "A failure here is logged and left
non-fatal: the read-plane annotation routes do not need actors, so a task-plane outage must not
take the media surface down with it. **The task endpoints surface it as a 503 instead.**" They do
not — nothing consults the flag. Either implement the check or delete the flag and the comments;
code that documents a fail-mode it does not implement costs an operator an hour.

**The FAIL-emit cap is justified against a "30 s Dapr ack window" that does not exist on the cron
transport** (`sweep.py:38`, repeated at `:300-301`). **CONFIRMED · severity low.** The transport is
an input binding: `chart/templates/maintenance.yaml:15` is `type: bindings.cron` with only
`- { name: schedule, ... }` — no ack, no `maxDeliver`. `dapr-resiliency.yaml`'s `targets.components`
block ranges solely over `$subscribers` pubsub components via `include "lance.subPubsub"`; no cron
component is named. And the 30 s literal is provably the resiliency-**off** branch:
`dapr-component.yaml` renders `- { name: ackWait, value: "720s" }` under
`{{- if .Values.dapr.resiliency.enabled }}` and `- { name: ackWait, value: "30s" }` only after
`{{- else }}`, while `values.yaml` sets `resiliency: enabled: true`. Keep the cap; restate the
reason against constraints that exist — the 120 s cron interval (`values.yaml:821`,
`schedule: "@every 120s"`) and the 120 s termination grace
(`values.yaml:368`, `maintenanceTerminationGracePeriodSeconds: 120`), both of which the sweep
genuinely races.

**The single-flight guard cites a values key that does not exist**
(`services/maintenance/src/maintenance/api/routes.py:45`). **CONFIRMED · severity low.** `:45`
reads "…with `compactionReplicas=1` (values.yaml) this is cluster-wide single-flight."
`grep -rn "compactionReplicas" chart/ services/` returns **exactly one hit — that comment line
itself**. `chart/templates/maintenance.yaml:47` is a bare `replicas: 1` inside the maintenance
Deployment spec, with no `{{ .Values... }}` interpolation. The real guarantee is *stronger* than
the cited one (unconditional, not a tunable), but an operator grepping the named key finds nothing.

**The bronze-arrival head documents two ingest lanes and implements one**
(`services/medallion/src/medallion/services/ingest_trigger.py:53`). **CONFIRMED · severity low.**
The truncation is literal: `:41-42` reads "TWO ingest lanes share the head: the events\n    lane
(``bronze_dataset``) — the returned name is" — the second lane is simply missing from the
sentence. And `:54` is a one-key dict:
`expected = {project_namespace(project, settings.bronze_dataset): settings.bronze_dataset}`.
`grep -rn 'bronze\$pages' services/medallion/src/` returns nothing (the string exists only in
`viewer/` and `ingest/`), while the mover that would consume it **is** configured
(`chart/values.yaml:811`, `pages-to-gold-htr ... fromDataset: bronze$pages, subTopic: medallion.bronze`).
The chart/code drift is also real: `grep -rn ingest-iiif` finds `chart/values.yaml:677` and
`chart/templates/medallion.yaml:164` describing the route, while
`services/medallion/src/medallion/producer.py:119-128` mounts only health, produce, ingest_media,
the bronze-arrival subscription and train — and `grep -rn 'ingest_iiif\|ingest-iiif' services/medallion/src/`
returns nothing at all. (First-draft path slip: the file is `medallion/producer.py` at the module
root, not under `services/`.) The truncated docstring is the tell that a lane was cut and the head
it fed was left describing it.

**The producer's app-id is `lance-ray`** (`chart/values.yaml:765`, `daprAppId: lance-ray`).
**CONFIRMED · severity low — a verbatim restatement of §1.3 item 2, not a new finding.** The
propagation is real and greppable: `chart/templates/medallion.yaml:140`
`- { name: MEDALLION_DLQ_TOPIC, value: "dlq.lance-ray" }`; `dapr-resiliency.yaml` appends
`.Values.medallion.producer.daprAppId` into `$subscribers`, which drives both `targets.components`
and the CRD's own `scopes:`. Renaming touches component scopes, DLQ topic names, resiliency
targets and the actor/state scope list, and the state store cannot hot-reload (§2.21) — so it is
one coordinated rollout. Worth doing once, deliberately, not opportunistically.

---

## 4. The maintenance-service Workflow candidate

**Verdict: No — not the sweep. Fix §2.19 and §2.20 instead. Revisit Workflow for
`purge_expired_trash` alone, later.**

**CONFIRMED · the verification could not find a load-bearing premise that fails.** One citation
correction is folded in below and marked.

**A workflow buys durable resumption of a plan. The sweep has no plan worth resuming.**
`run_sweep` (`services/maintenance/src/maintenance/services/sweep.py:94`) re-derives its entire
work list from object storage on every tick — `discover_datasets` walks each bucket fresh
(`optimize.py:68`) and the warehouse registry is re-read at `sweep.py:134`
(`registry = warehouse_records.list_warehouse_records(...)`) precisely because a config-time list
is stale by construction. Every per-dataset step is a Lance operation that is a no-op when there is
nothing to do. Losing a pass costs *latency*, not correctness; the next tick at `@every 120s`
(`chart/values.yaml:821`) redoes it.

**Contrast the plane that did adopt Workflow — with the citation corrected.** `services/ingest`
resumes because losing a pass loses **enumeration**: units 5,000-10,000 of a run were never
published and nothing will redeliver them. That sentence exists in `docs/DECISIONS.md` verbatim
but at **`:686-687`, not `:672`**, and its own paragraph argues the **opposite** of what the first
draft borrowed it for. In full, inside a **SUPERSEDED** entry: "the honest gap it addresses is
**enumeration**: if the API pod dies at unit 5,000 of 10,000, the remaining units were never
published, and unlike every other step that failure is not a message anything will redeliver.
**But the fix is chunking, not an engine**", closing "A workflow engine would buy convenience here,
and convenience is not the criterion." (`DECISIONS.md:672` is a row of the idempotency table,
unrelated. And the chunking sentence is at `:644`, not `:646`.) **This does not damage the
verdict — it strengthens it:** the estate has already ruled that an enumeration gap alone does not
justify an engine. Anyone chasing the old citation would have concluded the reverse.

**Are the steps replay-safe? The activities would be; the orchestrator would not.** Per step, all
convergent: `compact_files` is one Lance rewrite transaction (`optimize.py:177`);
`optimize_indices` is explicitly idempotent (`:194`, `:202`); `cleanup_old_versions` deleting an
already-deleted version is a no-op and `older_than` is wall-clock relative so a re-run deletes
strictly *more* — monotone, not divergent (`:248`); `enable_auto_cleanup` is a config write
(`:233`); the cadence stamp is written only on success (`sweep.py:236-239`,
`if policy is not None and policy.get("compact_interval_hours") and result.error is None`). So
every step is a legal activity. But the **orchestrator body** is full of hazards Dapr replays, and
they are exactly the right ones: `now = datetime.now(UTC)` at `sweep.py:178` (driving both the
interval decision and the stamp written at `:239`), `random.shuffle(failed)` at `:324`, and the S3
listing itself. All of it moves to `ctx.current_utc_datetime` or into activities. That is a rewrite
of `run_sweep`, not a wrapper.

**The cost is lower than a greenfield adoption — and that cuts both ways.** Workflow is already
adopted by owner ruling (`docs/DECISIONS.md:617`), `dapr-ext-workflow>=1.18` is a real dependency
(`services/ingest/pyproject.toml:22`), and the actor-capable store exists
(`chart/templates/dapr-statestore.yaml:62`). Infrastructure is not the cost. What is:

- `maintenance` is not in `stateStore.scopes` (`chart/values.yaml:964-1011` lists `annotator`,
  `catalog`, `ingest`, `flows` only). Adding it drags the secret-store scope chain with it —
  `dapr-component.yaml:219` `{{- $secretScopes = concat $secretScopes .Values.stateStore.scopes }}`
  under "Derived from stateStore.scopes rather than restated" — and needs a rollout of every scoped
  app, which §2.21 makes mandatory rather than advisory.
- **Every dataset result becomes durable history, every 120 seconds, forever.** Ingest pays that
  per *run*; maintenance would pay it per *tick*. Ingest's answer was chunking
  (`docs/DECISIONS.md:644`); maintenance would need the same plus a purge policy — which does not
  exist (see §2.23). **This is the dominant new cost and it recurs.**
- Two schedulers. The cron binding still fires and the workflow starts from it, so you need a
  deterministic per-tick instance id so a tick landing on a running instance is a no-op, plus
  purge, plus a management surface nobody asked for.

**What the premise is really describing is §2.19 and §2.20, and a workflow would mask them rather
than fix them.** Both fixes are ~30 lines total: shuffle or persist a rotation offset, and emit a
started counter. That delivers the *coverage* guarantee the durability argument actually wants,
with no state store, no replay determinism, and no per-tick history growth. And until §2.20 lands,
**nobody can measure how often a pass is actually lost** — the durability argument has no evidence
behind it in either direction.

**Where a workflow would earn it here: the purge, not the sweep.** `purge_expired_trash`
(`services/maintenance/src/maintenance/services/purge.py:433`) is the one genuinely ordered,
multi-step, **irreversible** sequence: revoke FGA tuples → delete bytes → clear record → announce
(`purge.py:14-25`). Its own code names the failure a workflow exists for — `purge.py:396-397`:
"the bytes were deleted and the grants revoked, but the trash record could not be cleared … the
next tick retries idempotently". That is a half-applied destructive operation whose completion
depends on the next cron tick happening at all, on a pass whose gate (`report_is_clean`,
`purge.py:172`) may never be satisfied again. Even so: it is idempotent by record id, the record
survives every failure, the per-tick cap bounds the blast radius (`purge.py:481`), and it **ships
off** (`chart/values.yaml:862`, `trashPurge: false`). So not now — but this is the piece to reopen,
and it is a bounded per-record child workflow, not a whole-estate sweep.

---

## 5. Dapr Workflow over Ray jobs

**Verdict: worth it narrowly.** Scope any adoption to the **train path**
(`services/medallion/src/medallion/services/ray_submit.py:112`) plus future multi-job
promotion. **Not** the stage movers — those are minutes long, idempotent, and their catalog
commit plus publication event already carries completion. And the whole thing is contingent on
Phase 2 of the spike below, which can kill it.

### 5.1 What Ray already guarantees — do not pay twice for these

Read from `docs.ray.io` (`ray-core/fault-tolerance.html` and its tasks / objects / actors / nodes /
gcs sub-pages, plus Ray Jobs submission):

- **Non-actor task retry on system failure, 3× by default.** "The default number of retries is
  3 and can be overridden by specifying max_retries."
- **Lineage reconstruction is on by default** for task-produced objects: Ray looks for copies
  on other nodes, then re-executes the originating task.
- **Worker-node and raylet failure are recoverable classes.** A dead raylet is folded into node
  failure and the same machinery re-runs the work on survivors. No external orchestrator needed.
- **Job submission is decoupled from the submitter's connection**: a job "runs once to
  completion or failure, regardless of the original submitter's connectivity". You do not need
  a durable process merely to hold a socket.
- **Actor restart and actor-task retry exist as first-class knobs** (`max_restarts`,
  `max_task_retries`) — off by default, but a parameter, not a wrapper. If the HTR
  actor-per-stage pipeline wants at-least-once, that is an edit to
  `runners/htr/src/runner/pipeline.py`, and no workflow can substitute for it.
- **GCS fault tolerance survives a GCS restart without killing in-flight work** — with HA Redis,
  and "officially supported only if you are using KubeRay".

### 5.2 Where Ray stops — the gap that is job-submission-shaped

- **"When a head node fails, the entire Ray cluster fails."** GCS FT preserves cluster metadata
  so a *new* head can come up; nothing inside Ray survives to resubmit.
- **"Jobs are bound to the lifetime of a Ray cluster, so if the cluster goes down, all running
  jobs on that cluster will be terminated."** No job-level checkpoint, no resume, no partial
  progress.
- **Ray delegates job-level retry to the caller, by name:** *"Retries or different runs with
  different parameters should be handled by the submitter."* This is the single most
  decision-relevant sentence in the Ray docs for this question.
- **Application exceptions are not retried** (`retry_exceptions=False` by default). The 3 free
  retries buy nothing against a bug or a bad IIIF response.
- **Owner failure is unrecoverable** ("Currently, Ray does not support recovery from owner
  failure") — a driver crash makes the objects it owns permanently unrecoverable.

### 5.3 What a workflow would actually buy

1. **Durable ownership of the job's outcome after the ack** — the one genuinely missing thing.
   Today every path is submit-and-ack (A13, 2026-08-03): the handler submits and acks, and
   nothing holds the intent past that point. Ray head dies at minute 40 of an HTR job → the
   trigger is long acked, the job is gone, and the only thing that notices is the B4
   storage→graph reconcile **cron sweep** reading Lance versions off disk
   (`docs/RESILIENCE.md` gap #1). That sweep *detects* the hole; it does not re-drive the work.
2. **A durable place for job-level retry with a changed plan** — not "retry the failed task"
   (Ray's, free) but "the cluster went away, resubmit" / "it OOM'd, resubmit with a smaller
   batch". No component owns that today. `submit_train_job` returning `"already_failed"` so the
   handler can DROP (`ray_submit.py:129`) is a dead end with no durable record behind it.
3. **A queryable per-run resource for a Ray job that survives the cluster.**
   `get_workflow_state` (`services/ingest/src/ingest/__init__.py:250`) plus
   `ctx.set_custom_status` (`workflow.py:201`) gives this free. `GET /api/ray/jobs` is cluster
   truth, and cluster truth is gone when the cluster is.
4. **Cross-job sequencing, fan-in and compensation** — Ray Jobs have no cross-job primitive at
   all. This is the silver→gold quality-promotion case that `docs/OPERATORS.md` itself named as
   the reopen trigger (`docs/DECISIONS.md:705` — citation re-verified, correct).
5. **A guaranteed terminal record** — subject to §2.3 being fixed first, since `emit_terminal` is
   today structurally skippable on the failure path. Note that it is *deliberately* not an outbox
   in the delivery sense (§2.2, §2.22): I8 makes the emit best-effort on purpose, so "guaranteed"
   here means "the terminal step runs", not "the event lands".

### 5.4 What it does not buy

- **Durability that the submission happens.** Already covered, and not by a workflow: the
  intent is a JetStream message on a durable consumer (`deliverPolicy: new` + `durableName`,
  `chart/templates/dapr-component.yaml`), redelivered by the Resiliency CRD (30 s → 300 s, 5
  attempts) and parked on `dlq.*` if poison. Chaos-verified live: published while the mover was
  scaled to 0, retained as `Unprocessed: 1`, delivered on recovery (`docs/RESILIENCE.md` gap
  #3). Mover dies before submitting → the trigger comes back.
- **Durability of the execution itself.** A workflow **cannot** resume a Ray job from step 4 —
  Ray has no job-level checkpoint. "Durable" here means durable **re-execution from the top**,
  which is only sound because rask's jobs are already idempotent (stage jobs `merge_insert` on
  a stable hash of `source_uri`; unit ids are content-derived). That idempotency is the
  **precondition** that makes a workflow safe, not something it provides.
- Task retry on worker/node death; lineage reconstruction; raylet recovery; actor restart; GCS
  restart survival — all Ray's or infra's (§5.1).
- **Idempotent submission under redelivery** — already owned by
  `ray_kit.submit.submission_id` (`packages/ray-kit/src/ray_kit/submit.py:53`) and
  `submit_or_reattach` (`:86`), including the reattach-or-resubmit-after-terminal-failure
  branch. A workflow inherits this; it does not add it.

### 5.5 Four concrete costs in *this* repo

1. **Retry double-count.** `ACTIVITY_RETRY` (`workflow.py:85`) is 4 attempts at 5 s ×2 backoff.
   Put a Ray submit under it naively and a transport blip yields 4 submissions in ~35 s while
   `submit_or_reattach` deletes-and-resubmits any prior FAILED job — the deterministic id is the
   only thing between that and two concurrent jobs racing one Lance write. The activity retry
   must cover **transport only**, and job failure must be a **returned value**, exactly as
   `flows.activities.run_node` does it (`services/flows/src/flows/activities.py:41`).
2. **Two owners of one trigger.** The JetStream durable consumer redelivers *and* the workflow
   holds the run. Either the workflow becomes the sole consumer of that trigger, or both drive
   one submission id.
3. **A monitor is a poll in a durable-timer costume.** A13 deleted the completion poll from
   three places on 2026-08-03 because "the poll asked a question the data already answers — a
   job's completion signal is its own registered commit". `ctx.create_timer` is explicitly
   carved out (`services/ingest/tests/test_poll_reason.py:29`,
   `tests/test_run_deadline.py:66`), so a durable timer is legal *by the letter*; but
   `tests/unit/test_ingest_invariants.py::test_a13_no_completion_polling_survives` bans the
   completion-poll literal repo-wide, and a 30 s status activity is that doctrine one layer up.
4. **History cost** lands on the single-replica CNPG Postgres that also holds the lineage graph
   (`docs/RESILIENCE.md` gap #5).

### 5.6 The shape, if it is built

**Host it in `ingest`, not `compute`.** `ingest` is already a workflow host with
`stateStore.scopes` granted, the runtime started (`__init__.py:166`), and `ray_dashboard_url`
already in shared settings (`packages/service-kit/src/service_kit/config.py:29`, alias
`RAY_DASHBOARD_URL`). Zero chart changes, zero new scopes, zero new sidecars. Move it to
`compute` only after the spike passes — and note that doing so means adding `compute` to
`stateStore.scopes` (`chart/values.yaml:964`), without which the sidecar logs "Actor state
store not configured - actor hosting disabled" and every schedule 500s (measured 2026-08-05,
documented at `values.yaml:987`). §2.21 makes that addition a rollout of every scoped app.

**Submit activity.** `submit_ray_job(ctx, payload) -> str` wrapping the two functions that
already exist (`ray_kit/submit.py:53`, `:86`). Already replay-idempotent by construction.
Nothing new needed.

**The wait — two shapes, and the choice is not free.**

- *(a) Durable timer + status activity (monitor).* New `job_status(ctx, sub_id)` doing
  `GET /api/jobs/{sub_id}` on the dashboard. Add it to `ray_kit/submit.py`; do **not** reuse
  `ray_kit.dashboard.list_jobs`, which materializes up to `MAX_JOBS=200` from a response
  measured at 81,155 jobs / 164.7 MB (`dashboard.py:49-56`). `continue_as_new` is not optional
  here. **UNVERIFIED:** "~500 history rows per multi-hour job at 30 s ticks" is arithmetic from
  the tick interval, not a measurement.
- *(b) External event.* `when_any([wait_for_external_event("ray_job_done"), create_timer(deadline)])`,
  raised from outside via `raise_workflow_event`. **The constraint:** Ray pods have no Dapr
  sidecar (`services/medallion/src/medallion/services/ray_submit.py:148`, "D2: no Dapr sidecar
  on Ray pods"), so the job cannot signal itself. The raiser must be a sidecar-bearing pod
  subscribing to the completion signal that already exists — the job's catalog commit →
  publication event. **The bridge is the deliverable**, and this repo has shipped exactly this
  bug once: `chunk_run`'s first version suspended on a Dapr external event while
  `worker.signal_drained` published to a NATS subject with no bridge between them, so every
  chunk would have waited the full fallback and reported zero fragments
  (`services/ingest/src/ingest/workflow.py:312-320`).

**Correct shape: (b), with (a)'s timer as the fallback leg of `when_any` — never a bare
status-poll monitor.** `ingest_run` already uses exactly this idiom (`workflow.py:277`) — and
§2.4 is the bill for using it carelessly. Note §3's finding that the losing leg cannot be
cancelled: whichever leg loses, its timer record is not withdrawn.

**Out of the workflow body for determinism:** every httpx call, `datetime.now()`/`time.time()`,
`uuid4`, `random`, and the terminal-state decision. The body may branch only on an activity's
returned value. `submission_id` must derive from `ctx.instance_id` + payload, never be minted
in the body. And do not copy the §2.23 env-in-workflow-scope trap into a Ray timeout constant —
§3's `RunSpec`/`ChunkSpec` item is the verified version of that hazard.

### 5.7 The spike

**Phase 1 — free, runs today, no cluster (~30 min).** Proves the *shape*; cannot prove
durability.

New files:

- `services/ingest/src/ingest/ray_probe.py` — `ray_probe(ctx, payload)`:
  `sub_id = yield ctx.call_activity(submit_ray_job, input=payload, retry_policy=TRANSPORT_ONLY_RETRY)`,
  then `winner = yield wf.when_any([ctx.wait_for_external_event("ray_job_done"), ctx.create_timer(timedelta(minutes=30))])`.
  Activities `submit_ray_job` and `job_status`. Annotate it
  `-> Generator[Any, Any, dict[str, Any]]` — see §3; do not copy ingest's wrong annotation.
- A `GET /api/jobs/{sub_id}` helper in `packages/ray-kit/src/ray_kit/submit.py` (~10 lines). Not
  `list_jobs`.
- `services/ingest/tests/test_ray_probe.py` — copy the generator-driver harness verbatim from
  `services/flows/tests/test_workflow.py:60-95` (`_FakeContext` recording `call_activity`, then
  `generator.send(reply)` in a loop).

Assert exactly three things:

1. Driving the generator twice over the same history dispatches the **same** `submission_id` —
   replay determinism, the property everything else rests on.
2. The timer leg returns a FAILED outcome **without** a second submit (drive `when_any` to pick
   the timer, assert `len(ctx.dispatched) == 1`).
3. Structural, mirroring `services/ingest/tests/test_run_deadline.py:66`: `ast`-parse
   `ray_probe.py`, assert `create_timer` and `when_any` appear in the workflow function and
   `asyncio.sleep` / `time.sleep` / `httpx` appear nowhere in it.

`cd /home/blackwell/Desktop/rask-ingest && uv run pytest services/ingest/tests/test_ray_probe.py -q`.
If this fails, stop — the shape is wrong and nothing downstream matters.

**Phase 2 — the kill test (~1 h, needs the cluster).** This is the one that decides; Phase 1 is
only its precondition. Wire a temporary `POST /v1/ray-probe` in
`services/ingest/src/ingest/api.py` scheduling `ray_probe` with
`{"stage":"spike","token":"t1"}` (copy `_DaprWorkflowStarter.start`, `__init__.py:227`).

```
make k3s-up && make ray-up
kubectl -n <ns> exec deploy/<ingest> -- curl -sXPOST localhost:<port>/v1/ray-probe -d '{"stage":"spike","token":"t1"}'
# while it is waiting on the event:
kubectl -n <ns> delete pod -l app.kubernetes.io/component=ingest
# after the pod is Ready again:
kubectl -n <ns> exec deploy/<ingest> -- curl -s http://<ray-head>:8265/api/jobs/ \
  | jq '[.[]|select(.submission_id|startswith("ray-spike-t1"))]|length'
```

**Test A (app-pod death).** PASS = exactly **1** job with that submission id, and
`get_workflow_state("<instance>")` reads RUNNING then COMPLETED. **2 = the idea is dead as
written** — `submit_or_reattach` does not hold under workflow replay and you have two jobs
racing one Lance write.

**Test B (the actual claim — kill the head, not the app):**
`kubectl -n <ns> delete pod -l ray.io/node-type=head`.
PASS = the workflow observes the job vanish and resubmits **once** against the new head.
**KILL** = it hangs until the 30-minute timer, because nothing publishes "your job died" — a
job's completion signal is its catalog commit, and a dead job commits nothing and rings
nothing. In that outcome the workflow bought a **timeout**, not a **resubmit**, and you have
rebuilt the B4 reconcile sweep with more moving parts and a second scheduler. That is the
specific failure to watch for, and it is the same shape as the `chunk_run` bridge bug this repo
already shipped.

**Also check while you are in the cluster:** whether the ingest pod's egress reaches the Ray
dashboard under `networkPolicy.enabled` — `chart/templates/network-policy.yaml` enumerates Ray
clients by component label and `ingest` is **not** among them. UNVERIFIED whether this blocks
the spike; it was read, not exercised.

---

## 6. Open questions

Three of the first draft's seven are now answered and have been struck.

1. ~~Do the secret-store scopes really derive from `stateStore.scopes`?~~ **ANSWERED.**
   `dapr-component.yaml:219` is `{{- $secretScopes = concat $secretScopes .Values.stateStore.scopes }}`
   — a concat onto hardcoded branches, not a replacement. Both readings were half-right; no
   anomaly. See §1.2.
2. ~~What is the actual `lance-statestore` hot-reload error?~~ **ANSWERED, live 2026-08-07.**
   Captured verbatim in §2.21; it is upstream Dapr behaviour for any actor state store, by design.
   Nothing to file.
3. ~~What is the runtime symptom of the 3500-to-gRPC-client bug?~~ **ANSWERED.** There is none:
   the seam has no consumer at all and gRPC channels are lazy. The medallion builds its own
   correct client. Dead-code hygiene — §2.1, §2.22.
4. **Should the two `medallion.bronze` movers be one app, or should the double-cascade be
   accepted in writing?** §2.12 forces the choice, and §2.11's DLQ fix is different in each
   case. **See `open_ingest_design.md` §4** — its recommendation (retire the lineage head, make
   the `published` tag the single trigger) dissolves the question rather than answering it.
5. **Does the ingest → Ray-dashboard egress pass under `networkPolicy.enabled`?** §5.7.
6. **Is `app-id: lance-ray` worth renaming now, or does it ride the next state-store change?**
   The rename touches component scopes, DLQ topics, resiliency targets and the scope list, and
   the state store cannot hot-reload — so it is one coordinated rollout either way.
7. **Purge policy for workflow history.** Nothing collects it today (§2.23), and both §4 and
   §5 make it worse before they make it better. Decide the retention rule before adding a
   second workflow host.
8. **What is the measured size of an `enumerate_chunks` result at the advertised scale?** §2.13's
   structure is confirmed but its magnitude is not establishable from source — no gRPC limit and
   no state-store row limit is set anywhere in this repo. Measure before sizing the fix.
9. **Does an orphaned `drain_chunk` fail loudly or silently after §2.4 deletes its consumer?**
   Needs a run, not a read. It decides whether the deadline path is noisy-broken or
   silently-broken.
