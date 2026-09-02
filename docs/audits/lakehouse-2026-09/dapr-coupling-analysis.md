# How hard-bound rask is to Dapr, and what leaves with it

Measured 2026-09-02 against `origin/main` at `9363bf3` (the authoritative tree). The local `main`
at `feec956` was measured too; it differs by a few percent on every count and by no conclusion.
Every number below is a grep or line count you can re-run, not an estimate.

**No code was changed.** This is a read-only measurement whose deliverable is the coupling map and
the loss list.

---

## 1. The verdict in four sentences

Roughly **7% of the Python estate is written IN a Dapr programming model** (actors and workflows,
~5,900 lines) and cannot be ported, only rewritten. A further **~30% of source and ~30% of tests
name Dapr** but sit behind seams that already have non-Dapr siblings, so they are rewired, not
rewritten. The chart is **~70% Dapr-touched by template count**, but almost all of that is
deletion. The part with no fallback anywhere in the repo is the **service-to-service security
model**: mTLS, app-token delivery auth and per-app-id component scopes are all Dapr, and removing
it removes them wholesale.

---

## 2. The numbers

### 2.1 Python source

| measure | value | share |
| --- | ---: | ---: |
| source files | 480 | |
| source lines | 93,121 | |
| files importing the Dapr SDK | 40 | 8% |
| files naming Dapr in any way (SDK, sidecar HTTP, env, comments) | 171 | 36% |
| workspace members declaring a `dapr*` dependency | 9 services + `service-kit` | 10 of 21 |

The 40 SDK-importing files split cleanly by kind:

| kind | files | lines | what it is |
| --- | ---: | ---: | --- |
| actors | 9 | 2,481 | annotator `AnnotationTaskActor` / `AnnotationProjectActor` / `TenantProjectsActor`, notifications `InboxActor` / `WatchActor`, the typed proxies, warmup, the state-store probe |
| workflows + activities | 7 | 3,407 | `ingest/workflow.py` (1,509), `medallion/workflow.py` (1,563), `flows/{workflow,activities,runtime}.py`, `service_kit.activity_loop` |
| pub/sub glue | ~15 | ~900 | `dapr_publish`, `control_emit`, `bus_metrics`, `dapr_auth`, `secrets`, `catalog/api/dapr.py`, `lineage/api/dapr.py`, the medallion `api/*` routers |
| everything else | ~9 | small | one-line sidecar URL builders, `apply_dapr_secrets`, lifespan wiring |

**The programming-model tier is ~5,900 lines, 6.3% of source.** That is the part with no seam.

### 2.2 Sidecar-only environment and headers

| symbol | occurrences | meaning if the sidecar is gone |
| --- | ---: | --- |
| `DAPR_HTTP_PORT` / `DAPR_GRPC_PORT` | 20 | every sidecar-address computation |
| `DAPR_SECRET_*` | 14 | the secret-store read path |
| `DAPR_ENABLED` / `RASK_DAPR_ENABLED` / `LINEAGE_DAPR_ENABLED` | 6 | the toggle that already exists for the no-sidecar dev loop |
| `dapr-api-token` header | 22 | delivery auth on every subscription and binding route, plus the BFF and gateway forwarding it |
| `DAPR_APP_ID_SEPARATOR` | 8 | the `||` key-prefix workaround in `user_state.py` |

### 2.3 Tests

| measure | value | share |
| --- | ---: | ---: |
| test files | 769 | |
| test files naming Dapr | 240 | 31% |
| test files importing the Dapr SDK | 35 | 5% |
| invariant tests named for Dapr (`tests/unit/test_dapr_*`, `test_lineage_dapr_delivery`) | 4 | 20 test functions |
| e2e-py suites that need a live sidecar | 8 | maintenance, media, dummy lane, governance, user state, outbox crash, observability, Ray train |

### 2.4 Chart

| measure | value |
| --- | ---: |
| templates | 53 |
| templates naming Dapr | 36 |
| Dapr-specific templates (`dapr-app-token`, `dapr-component`, `dapr-dashboard`, `dapr-inject-sweep`, `dapr-resiliency`, `dapr-statestore`) | 6 |
| templates emitting sidecar annotations via the `_helpers.tpl` block | 16 |
| `values.yaml` lines naming Dapr | 127 of 2,599 |
| `Component` CRs | 17 |
| `Configuration` CRs | 1 (tracing) |
| `Resiliency` CRs | 2 (pub/sub delivery, invocation) |
| scoped components (per-app-id authorization) | 10 |
| Dapr control-plane pods the subchart runs | operator, injector, sentry, placement, scheduler (5 images) |

Component types: 7 `bindings.cron`, 1 `bindings.http`, 1 `bindings.smtp`, 6 `pubsub.jetstream`,
1 `secretstores.hashicorp.vault`, 1 `state.postgresql` (`actorStateStore: true`).

### 2.5 Observability

| measure | value |
| --- | --- |
| `dapr_*` metric families referenced by alert rules | 10 |
| alert rules on those metrics | 14 lines |
| alerts named for Dapr, workflow or actor | `DaprConsumerWedge`, `InboxActorTurnQueueBacklog`, `DaprSchedulerServingNoSidecars`, `WorkflowActivitiesFailing`, `WorkflowEngineWedged`, `DaprSchedulerMetricsMissing` |
| OTel Collector | a daprd-specific filelog parser; a sidecar-metrics scrape on the `dapr-metrics` port |
| dashboards | 12 Perses references; a Dapr dashboard template (81 lines) |

### 2.6 Documents, rulings, tooling

| surface | value |
| --- | --- |
| `docs/` pages naming Dapr | 40 of 86 |
| `docs/architecture/` pages naming Dapr | 8 |
| `docs/DECISIONS.md` headings whose body names Dapr | 9 of 44 |
| `docs/OPERATORS.md` mentions | 25 |
| project skills naming Dapr | 5 of 8 |
| Makefile references | 8 (helm repo, PVC cleanup, the 5 images, an e2e target) |
| scripts naming Dapr | 13 |
| `.dagger/` files naming Dapr | 2 (chart-render gates; the mailpit rig) |
| frontend files naming Dapr | 17, of which 2 forward the `dapr-api-token` header and 15 are comments or generated `/dapr/subscribe` types |
| open backlog | `open_dapr-audit.md`: 48 verified findings, 7 critical, all on the workflow/actor/pub-sub surface |

---

## 3. Three kinds of coupling, and why the distinction matters

Counting files overstates the problem and counting SDK imports understates it. The honest cut is by
*kind*.

### A. Programming-model coupling: rewrite

Code whose **shape** is Dapr's. Turn-based actor concurrency, reminders, replayed workflow
generators, `call_child_workflow`, `wait_for_external_event`, `continue_as_new`. There is no seam
because the abstraction is the design. ~5,900 lines, 16 files, plus roughly 90 test files that
exercise them.

### B. Transport coupling: rewire

Code that **calls** the sidecar, behind a seam that already has a non-Dapr sibling:

| seam | Dapr impl | non-Dapr siblings already in the tree |
| --- | --- | --- |
| `lineage_kit.Emitter` | `ClientEmitter` (via HTTP) | `NoopEmitter`, `RecordingEmitter` |
| `catalog.core.lineage_emit.LineageEmitter` | `DaprEmitter` | `HttpLineageEmitter`, `NoopEmitter`, `OriginatorBoundEmitter` |
| `service_kit.control_emit.ControlEmitter` | `DaprControlEmitter` | `NoopControlEmitter` |
| `maintenance.core.lineage_emit.MaintenanceEmitter` | `DaprMaintenanceEmitter` | `NoopEmitter` |
| `annotator.projects.saga.Publisher` | injected | test fakes |
| `notifications.api.channels` binding client | `invoke_binding` | Protocol, test fakes |
| `service_kit.governed.secrets` | `fetch_dapr_secret` | one function |
| `service_kit.governed.user_state` | sidecar state API | one module |
| gateway invocation | `/v1.0/invoke/{app_id}/method` | one URL builder gated on `RASK_DAPR_ENABLED` |

Publish is genuinely decoupled. **Subscribe is not.** There are 25 `@dapr_app.subscribe` routes
across catalog (1), lineage (2), medallion (5 registrations, more routes), notifications (2). Each
is a plain FastAPI route the sidecar POSTs a CloudEvent to, guarded by `require_dapr_token`.
Replacing the sidecar means each becomes an in-process consumer with its own lifespan task, its
own ack discipline, and its own auth. That is rewiring, not rewriting, but it is 25 of them.

The ingest work queue is the proof this rewiring is feasible: `ingest/queue.py` is a direct
nats-py JetStream pull consumer, documented as the one exception, and it hand-rolls exactly what
the sidecar otherwise provides (`park_poison`, `ensure_dlq_stream`, `max_ack_pending`, the
per-run durable). It cost ~577 lines plus ~584 for the worker.

### C. Infrastructure and contract coupling: replace or lose

Not code. Things the estate relies on that exist only because a sidecar sits beside every pod:

| what | where | what it does |
| --- | --- | --- |
| **mTLS** | Sentry, pinned on (`values.yaml:1947`) | every service-to-service call is encrypted and identity-bound |
| **delivery auth** | `dapr.io/app-token-secret` → `APP_API_TOKEN` + `dapr-api-token` header; `require_dapr_token` | a forged CloudEvent POSTed to `/lineage-events` is refused; the security audit called this a prod-blocker |
| **component scopes** | 10 scoped components | only the listed app-ids may publish to a topic, read a state store, or resolve a secret |
| **resiliency** | 2 CRs | pub/sub: exponential retry 30s→300s ×4 then DLQ, per subscriber app-id; invocation: 300s timeout, ×3 retry matched on 408/429/5xx (the circuit breaker was removed on purpose, with a measured DoS as the reason) |
| **placement** | control plane | routes `AnnotationTaskActor/<id>` to exactly one pod cluster-wide; `values.yaml:1954` calls it load-bearing |
| **scheduler** | control plane, etcd-backed | backs actor reminders (task-lease expiry) and workflow timers |
| **tracing** | `Configuration` CR + `service_kit.otel` injecting `traceparent` into sidecar calls | one distributed trace across the bus for free |
| **injection sweep** | `dapr-inject-sweep.yaml` Job | works around the helm-ordering race where pods are admitted before the injector webhook exists |
| **network policy** | `network-policy.yaml` | rules for the control plane's API-server egress and the sidecars' OpenBao egress |
| **discovery endpoints** | `GET /dapr/subscribe`, `/dapr/config` | mounted by `DaprApp`, exported into the generated TypeScript clients |
| **rulings** | 9 DECISIONS headings, OPERATORS §4 | the outbox ruling, the workflow adoption, the actor-boundary ruling, the "no distributed lock" ruling |

---

## 4. Block by block: what leaves, what replaces it, what is lost

### 4.1 Pub/sub

**Where.** 6 `pubsub.jetstream` components; 25 subscription routes; every publish site funnels
through `service_kit.dapr_publish.publish_event`, which is also where the claim-check payload
guard lives (900 KiB hard cap, 64 KiB warn).

**Replacement.** nats-py, already a dependency of ingest.

**Lost, and what it becomes.**

- Sidecar redelivery and DLQ parking per the Resiliency CR → JetStream consumer config
  (`max_deliver`, `ack_wait`) plus an explicit DLQ publish. Ingest already carries this pattern.
- The CloudEvent envelope and the `dapr-api-token` forged-delivery guard → NATS authentication
  per service (credentialed NATS is already in the chart per `Chart.yaml:33`) and the envelope
  becomes your own schema.
- Component scopes → NATS account and subject permissions.
- `queueGroupName` per mover → JetStream durable consumer naming.
- W3C trace context through the sidecar's instrumented gRPC client → `traceparent` in NATS
  headers, injected by hand (`UnitTask.traceparent` in ingest already does this).
- The catalog's broadcast subscription (no queue group, every replica gets every event) → an
  ephemeral consumer per replica.
- The claim-check guard stays; it is application code.

**Effort class.** Moderate. The publish side is a few adapters. The subscribe side is 25 routes
becoming consumers, and each mover's `RETRY`-return-to-redeliver contract becomes a nak.

### 4.2 Actors

**Where.** 2,481 lines. Annotator: one actor per task (claims, leases, review), one per project
(publish saga driver with a reminder), one per tenant (index). Notifications: one inbox per
subject (claim-check pointers, read state, a compaction reminder), one watch actor.

**What they provide.** Placement (one pod owns an id), turn-based concurrency (the lock: two
annotators clicking Claim are serialised by the runtime, and the file says "do not add a
distributed-lock component for this"), durable reminders (lease expiry costs O(expiries) rather
than O(tasks)), per-actor state on the state store.

**Replacement.** JetStream KV compare-and-swap for the record plus NATS 2.14 scheduled messages
for reminders (the chart runs 2.14.2, which has `@at`, cron and `@every` natively) and per-key KV
TTL for leases. Or Postgres rows with `SELECT ... FOR UPDATE` plus scheduled messages.

**Lost.**

- Turn-based serialisation becomes optimistic CAS-retry. Every `@actormethod` becomes
  read-CAS-write. Correctness is equivalent; ergonomics regress, and every method body has to be
  written as retryable.
- Reminders as a first-class durable primitive with a `failure_policy`. Scheduled messages give
  the timer; correlating it to state and handling a missed tick is yours.
- Locality. An actor holds warm in-memory state between turns; a KV design reads on every turn.
- `actor_warmup`, the state-store probe, `InboxActorTurnQueueBacklog`, `dapr_runtime_actor_*`
  metrics.

**Effort class.** Rewrite. The model code, the proxies, and roughly 30 test files across the two
services.

### 4.3 Workflows

**Where.** 3,407 lines. `ingest_run` + `chunk_run`; `stage_run` + `promotion_review` +
`train`; `flow_run`.

**What they provide.** Durable timers, history replay, child workflows with caller-minted ids,
external events, pause/resume/terminate, `continue_as_new`, `set_custom_status`, workflow state
metrics, the `workflowRetention` purge.

**Replacement.** Another engine, or none if ingest becomes a library the user's engine drives.
The separate Flyte 2 audit covers this in depth.

**Lost.** Everything above; `WorkflowActivitiesFailing`, `WorkflowEngineWedged`,
`DaprWorkflowHistoryNotCollected`, `DaprWorkflowStateMetricsMissing`; ~80 test files.

**Effort class.** Rewrite, or delete if the plane leaves the product.

### 4.4 Secrets

**Where.** `fetch_dapr_secret` (one function, retry-while-seeding, fail-closed, 4xx is
misconfiguration), `apply_dapr_secrets` in medallion, and the chart omitting the value from pod
env when `secrets_from_dapr` is on. One `secretstores.hashicorp.vault` component with a scope
list; the state store and the SMTP binding resolve their own credentials through it.

**Replacement.** `hvac` with Kubernetes auth at boot. The backend is already OpenBao.

**Lost.** The sidecar resolving component-level secrets (the state-store DSN, the SMTP password)
without any app code. Those move to whoever now owns the consumer. The scope list becomes OpenBao
policy.

**Do not** fall back to External Secrets rendering into env. The security audit moved secrets out
of env specifically; the ESO path exists in the chart but re-opens that finding.

**Effort class.** Small.

### 4.5 Bindings

**Where.** 7 cron (lineage reconcile, maintenance sweeps, incremental ingest, inbox digests), 1
http (Slack), 1 smtp (email). Each cron is a route the sidecar POSTs to, guarded by the same
delivery token.

**Replacement.** Cron → NATS 2.14 scheduled messages or a Kubernetes CronJob hitting the route.
http → httpx. smtp → aiosmtplib.

**Lost.** The "no scheduler thread in this service" property, which several docstrings state as a
design rule. A CronJob preserves it; an in-process scheduler does not. The forged-sweep guard on
the cron route needs its own auth.

**Effort class.** Small.

### 4.6 Service invocation

**Where.** The gateway builds `/v1.0/invoke/{app_id}/method` when `RASK_DAPR_ENABLED`; the
notifications reconciler reaches lineage the same way. Both forward `dapr-api-token`.

**Replacement.** Kubernetes Service DNS plus httpx retry.

**Lost.** mTLS on every hop; the invocation Resiliency policy; app-id addressing. The 4xx-aware
retry matching is trivial to reproduce. mTLS is not: it is a mesh, or accepted plaintext inside a
NetworkPolicy-fenced namespace.

**Effort class.** Small for code. mTLS is an infrastructure decision, not a code change.

### 4.7 State, non-actor

**Where.** `service_kit.governed.user_state`: dock workbench layouts and saved views per subject,
on `state.postgresql`, called from catalog endpoints and the notifications inbox. Carries a
`||`-encoding workaround for Dapr's key prefix.

**Replacement.** A table on the CNPG Postgres already there, or a Lance table.

**Lost.** ETag concurrency from the state API. The key-prefix workaround goes with it, which is a
gain.

**Effort class.** Small.

### 4.8 Cross-cutting

| what | after Dapr |
| --- | --- |
| mTLS | a mesh, or none. **Nothing in the repo provides this otherwise.** |
| delivery auth on 25 routes | NATS auth for bus deliveries; a real service credential for HTTP doors |
| component scopes | NATS permissions + OpenBao policy + Postgres grants, each maintained separately |
| the injection-race sweep Job | deleted; a real simplification |
| placement + scheduler pods and their etcd | deleted |
| `dapr_*` alerts and dashboards | rebuilt on NATS and app metrics; `DaprConsumerWedge` in particular has no direct twin |
| the Collector's daprd log parser and sidecar scrape | deleted |
| `/dapr/subscribe` in the generated TS clients | regenerated |
| 9 rulings, 40 doc pages, 5 skills | become historical; the outbox and "no distributed lock" rulings survive unchanged because they are not about Dapr |

---

## 5. What stays no matter what

None of these is Dapr-bound and none moves:

- the Lance catalog and the Lance Namespace surface, the hierarchy guards, protection, trash
- lineage into AGE and OpenFGA, both on CNPG
- the application-side outbox (`service_kit.lakehouse.outbox`), by the 2026-08-15 ruling
- NATS JetStream itself, OpenBao itself, GreptimeDB, Perses, KubeRay, Ray Serve
- the ingest work queue (already nats-py) and everything under `service_kit.lakehouse`
- every Protocol seam in §3.B and its non-Dapr siblings

---

## 6. Loss list, ranked by how hard it is to get back

1. **Service-to-service security.** mTLS, delivery auth and scopes are the whole model and the repo
   holds no alternative. This is the one item with nothing to swap in.
2. **Turn-based actor concurrency and durable reminders.** Replaceable with NATS 2.14 primitives at
   a real ergonomic cost, and every actor method has to be rewritten as a CAS loop.
3. **Durable workflow.** Replaceable only by another engine; not replaceable by NATS.
4. **Declarative resiliency and DLQ.** Becomes code in every consumer. Ingest's queue module shows
   the going rate: several hundred lines and two measured production bugs.
5. **Bus trace propagation.** Becomes manual header injection at every publish and consume.
6. **The operational surface.** Six alerts, a dashboard, ten metric families, a log parser.
7. **The injection race, the `||` prefix, the sidecar memory and startup cost, and the
   workflow-and-actor share of the 48-finding audit backlog.** These are gains.

---

## 7. What this means for the decision

The bound is real but it is not evenly spread, and the file counts hide where it actually bites.

- **If the question is "can the lakehouse core run without Dapr"**: yes, and the seams are already
  there. Catalog, lineage, maintenance, search, viewer, gateway and compute use only pub/sub,
  bindings, secrets and invocation. Every one of those has a one-module or one-function seam and a
  nats-py precedent in the same repo.
- **If the question is "can rask as it exists today run without Dapr"**: not without rewriting
  annotator, notifications, ingest, medallion and flows, which is ~5,900 lines of model code plus
  ~110 test files, plus a security model built from scratch.
- **The cheapest honest first move** is not removal. It is making one mover consume its trigger
  through nats-py behind the existing seam, with the delivery-auth question answered for that one
  path. That tells you whether the external-consumer contract holds before anything is deleted,
  and it leaves every ruling intact.
