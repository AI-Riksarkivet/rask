---
name: rask-dapr
description: "Dapr in rask: which of the 12 building blocks this estate uses (7) and why, which it deliberately does not (5) with the recorded rulings, and the component-vs-DIY rubric — when a Dapr component is the answer and when hand-rolling is justified. Use when adding a capability that smells like queuing/scheduling/locking/config/secrets/middleware, when tempted to hand-roll one of those, when a Dapr API seems to fit a new feature, or when touching sidecar annotations, Components, or the dapr-resiliency map."
---

# rask × Dapr — what we use, what we refused, and how to decide

The generic sources do not answer rask's questions: `diagrid-labs/dapr-skills` is workflow+agent
AUTHORING (its own README title; its prereqs even assume Docker, which rask forbids), and the Dapr
docs list the 12 building blocks without tradeoffs. This skill is the estate-grounded layer: every
claim below was verified against the render or the code (2026-08-28), not inherited from docs.

## The inventory — 7 of 12 blocks in use

| Block | Used | Where (verified) |
| --- | --- | --- |
| Service invocation | ✅ | notifications' reconciler → `/v1.0/invoke/lineage/method` (`feed_base_url`; direct URL when Dapr is off). `<release>-invocation-resiliency` (rendered with notifications) gives it `invokeRetry` (constant 2 s ×3, `408,429,500-599`) and `invokeTimeout` 300 s, targeting app `lineage`, `scopes: [notifications]`. No policy targets the catalog: no service invokes it through Dapr. Public traffic never rides invocation: the cluster route reaches each app port directly |
| State | ✅ | `state.postgresql` ×1 in the product chart (the actor state store, scoped to `catalog`, `flows` and `notifications`; scopes are load-bearing — see traps). The annotator's release renders its own, `rask-annotator-statestore`, scoped to app-id `annotator` alone on a CloudNativePG Postgres of its own (ADR 0171) |
| Pub/sub | ✅ | `pubsub.jetstream`, one Component per app-id that publishes or subscribes, each scoped to ONE app-id and naming that app's NATS user from the store (`jwt`/`seedKey` secretKeyRef to `nats-user-<app>`, `auth.secretStore: lance-secrets`; the permission table is `nats.auth.users` in values.yaml, [[XC-078]]); `DaprApp.subscribe` incl. a DLQ route; `queueGroupName` makes replicas competing consumers |
| Bindings | ✅ | `bindings.cron` ×7 in the product chart: `maintenance-cron` (the sweep), `maintenance-reconcile-cron`, `lineage-relay-cron` (drains the outbox, prunes the feed, forwards to the graph backend), `notifications-reconcile-cron`, `catalog-control-relay-cron`, `catalog-erasure-release-cron` (ADR 0138, releasing an erasure's pinning tags and branches when due), `catalog-promotion-expiry-cron` (ADR 0161, expiring promotion holds past their review deadline); the compute fixture chart (`deploy/fixtures/compute`, ADR 0155) renders a seventh, `compute-prune-jobs-cron` — read the cluster for what runs, not the templates. Plus `bindings.smtp`, `bindings.http` — all delivered to `POST /<component-name>` at the pod ROOT |
| Actors | ✅ | `DaprActor` ×17, `ActorProxy` ×28 (notifications' inbox and watch index on the product chart's store; the annotator's project and task actors on its own store, in its own release) |
| Secrets | ✅ | `secretstores.hashicorp.vault` → the platform's Vault or OpenBao (the dev OpenBao fixture in development; the chart renders none, [[XC-149]]). ONE component, `lance-secrets`, logs each sidecar in with Kubernetes auth as the role `<release>-{appID}` (Dapr 1.19+): the pod's own ServiceAccount, a token projected into daprd alone, a policy reading exactly that app's secrets; each app's Configuration is deny-by-default with `allowedSecrets` (`lance.storeReads`, ADR 0169). THE estate rule: sole source, fail-closed, never env fallback. Every read is held at most a jittered 300-600 s and read again (`service_kit.governed.secret_cache`, ADR 0166), so a rotation in the store reaches running pods; lineage's and maintenance's S3 key pair (both halves) is kept confirmed in the background (`store_credential`); the catalog holds none, its store identity being its ServiceAccount token (ADR 0170), and the app-token door accepts the current or previous token |
| Workflow | ✅ | `DaprWorkflowClient`, `WorkflowRuntime` in `services/flows` only. The lakehouse runs no workflow: the import contract `the-lakehouse-is-not-built-on-a-workflow-engine` forbids it (ADR 0154). The promotion gate is a tag, not a saga: readers read `@published`, approval moves the tag, rejection restores main, a cron sweep expires stale holds (ADR 0156 C1, ADR 0161, [[LH-466]]) |
| Configuration | ❌ | Ruled out — see refusals |
| Middleware | ❌ | Ruled out — see refusals |
| Distributed lock | ❌ | Not yet — the ONE open candidate (see below) |
| Jobs | ❌ | Cron bindings instead — revisit trigger recorded below |
| Crypto / Conversation | ❌ | No use case; Conversation would put an LLM key in a Component |

## The rubric — component vs DIY

**Reach for the Dapr component when ALL of these hold:**
1. **The surface has a sidecar.** Non-negotiable precondition — see the sidecar map below.
2. **The need is cross-cutting** (every service wants it the same way) rather than one route's semantics.
3. **Declarative Component YAML + rollout-restart is an acceptable change cadence** — this estate runs
   `HotReload: false` (measured: ~540 ERROR lines/hour from a reconcile loop that cannot converge), so
   a Component edit reaches sidecars only via `kubectl rollout restart`.
4. **The block's guarantee matches the need as SPECIFIED, not approximately** — e.g. Jobs is
   "at-least-once, no ceiling on lateness"; a lock is acquire-or-fail. If you must wrap the block to
   correct its semantics, that wrapper IS the DIY and the block bought you a dependency.

**DIY is justified when ANY of these hold (and the justification is written at the site):**
- **No sidecar on that hop.** The two standing examples: the PUBLIC edge (the platform's Gateway →
  each service's app port is direct, so no Dapr middleware/ratelimit can ever see a client request — rate limiting and
  body caps therefore live in `service_kit.middleware` / slowapi, by owner ruling 2026-08-26), and
  **Ray pods** (no daprd; a job cannot call `/v1.0/secrets/*`).
- **Per-request semantics the block cannot express.** notifications' reconcile guard must SKIP an
  overlapping tick, not queue it — an actor (turn-based) queues, so the estate keeps a process-local
  `asyncio.Lock` and a single replica until a cross-pod guard that skips exists.
- **The render is the gate.** rask's invariant tests read config out of `helm template`; state moved
  behind a runtime API (the Configuration block) is state those gates can no longer see.
- **The block is Alpha and the need is load-bearing.** Alpha components (lock, most middleware) may
  churn; a correctness-critical seam should not pivot on one without a recorded owner decision.

**Never DIY these, ever:** secret distribution (the store via the Dapr secret store is the sole source —
no env fallback, no "graceful degradation"; scope the Component to every consuming app-id), pub/sub
delivery/retry (JetStream + `dapr-resiliency.yaml`, never a hand retry loop around a publish), and
actor placement/turn semantics.

## Recorded refusals (do not re-litigate without new facts)

- **HTTP middleware pipeline** (2026-08-28, three independent reasons): public traffic never traverses
  daprd (the cluster route reaches the app port); `middleware.http.ratelimit` is per-sidecar and keyed
  on remote IP, which on a service-invocation hop is the calling pod, never the client; one
  `dapr.io/config` per sidecar + HotReload-off makes a pipeline fleet-global and rollout-gated.
- **Configuration API** (2026-08-28): the fleet's config is ONE ConfigMap via `envFrom`, validated by
  pydantic-settings at boot, and the render-driven invariant tests depend on seeing it; change
  cadence is deliberately rollout-shaped. It solves a problem this estate chose not to have.
- **Jobs API** (2026-08-28; reasons rewritten 2026-09-03 after four adversarial re-tests — the
  CONCLUSION held every time, most of the original reasoning did not). The six crons stay
  `bindings.cron`. **This entry is deliberately shorter than it was: three of the four counts it used
  to give are falsified, and a refusal defended by dead reasons invites the re-litigation it exists to
  prevent.**

  **The count that carries it, and it was never stated before:** the selection function is
  WHOLE-ESTATE and a per-table wakeup cannot compute it. `sweep.py::_protected_roots` must open every
  discovered dataset in every bucket before one is compacted — a shallow clone in bucket B is the only
  thing that knows bucket A's dataset must not be rewritten, and `base_refs.py` states why no
  per-dataset check can find it ("the evidence lives only on the referring side"). That pre-pass IS
  the scan's dominant cost, so a per-target Job avoids a datetime comparison and avoids nothing that
  is expensive — while either re-running the pre-pass per job (catastrophically worse) or skipping it
  (re-opening the measured #114/#128d data-loss defect). `work_queue.py` already carries the
  consequence: a worker "cannot recompute the whole-estate protection verdict", which is why
  `DatasetWorkItem` ships the reduced `protected_by`.

  Secondary, and still true: **the loop must exist anyway** for datasets carrying no policy — they
  have no `_policies/state/` record at all and are maintained on every tick — so Jobs would add a
  second mechanism beside the first rather than replacing it.

  **What was falsified, so nobody re-derives it from this file:** a tier table CAN carry a
  policy (a project record matches at bucket level, `maintenance_policies.py:18,156`); the "60x finer
  resolution" was demo-only arithmetic (prod is `0 */30 * * * *`, i.e. 2x); "per-target wakeups queue
  on the same lock" stopped describing the queue lane when N4 landed (`41f29fa8` — `routes.on_cron`
  holds `_sweep_lock` for PLANNING only and `api/work.py::handle_unit` takes no lock); and "creates a
  second stateful store" names the wrong thing — the Scheduler etcd is already deployed and
  load-bearing for Dapr Workflow and actor reminders, so what per-policy Jobs would create is a second
  authoritative HOME for policy cadence that every create/update/delete/drop/undrop path must mirror,
  invisible to the render gates and to the drift report's three store categories.

  **The event-driven redesign (2026-09-03) shrinks the question rather than reopening it.** With the
  primary trigger becoming a `lineage.events.v1` subscription and the cron demoted to an hourly
  backstop, the scheduling mechanism stops driving maintenance at all; the components that need
  throughput (the event subscriber, the N4 executor) scale on `queueGroupName` competing consumers,
  which needs no Jobs. **AUDITED ESTATE-WIDE 2026-08-28** (10 time-shaped clusters,
  adversarial, fable agents): **zero SHOULD_USE_JOBS.** Every hypothesised consumer dissolved on
  inspection — the candidates below are REFUTED and must not be re-proposed without new facts:
  * *auto-purge at trash deadline* → already shipped as a sweep flag (`MAINTENANCE_TRASH_PURGE_ENABLED`,
    report-only default is a recorded destruction posture). A purge tolerates a tick's lateness and
    must re-verify state at execution anyway (`purge.due_records` is the one shared rule), so the
    sweep is the CORRECT mechanism, not a stand-in.
  * *TTL grants* → already live as an FGA CEL condition (`non_expired_grant`, model.fga:481) —
    check-time evaluation, strictly better than revoke-at-T (no window a crashed revoker leaves open).
  * *per-subject digest one-shots* → actor reminders, correctly (the state lives in the actor's turn;
    reminders ride the same Scheduler Jobs would). The audit's ONE finding here was a repairability
    defect, not a mechanism error — fixed 2026-08-28 (`_digest_orphaned` read-path repair; Jobs has
    the identical two-store split and would not have helped).
  **The real revisit trigger:** a one-shot that is scoped to NOTHING that already holds state — no
  actor to remind, no workflow to time, no record a scan can evaluate. Example: an estate-wide digest
  at a fixed local time per timezone cohort. None exists. Calendar semantics in a per-entity policy
  ("02:00 in the project's timezone") are NOT it: still computable from data at each tick.

## Jobs vs Workflow vs reminder — where a DELAY belongs

One question decides it: **where does the state live?**

| The scheduled thing is… | Use | Why |
| --- | --- | --- |
| one step of a stateful multi-step process | **Workflow timer** | the process already holds the context (run record, idempotency key, what to emit on outcome); resumes mid-process after a crash |
| a wakeup for state living in an actor | **actor reminder** | runs under the actor's turn lock — the plane's own concurrency control; Jobs would land at the app and have to re-invoke the actor anyway |
| a bare future trigger with no surrounding state | **Jobs** | one etcd row + a callback; a workflow here would be an orchestrator used as an alarm clock (history rows, replay, determinism rules, zero orchestration) |
| recurring scan-and-converge | **cron binding** | as ruled above |

Two failure smells, one per direction: a workflow whose body is ONLY `create_timer` → should be
Jobs; Jobs callbacks that CHAIN (A schedules B schedules C, payload accumulating state) → a
hand-built workflow without history, replay or per-step retry — should be Workflow.

## The one open candidate

**Distributed lock** for the notifications reconciler. The exit condition is a cross-pod guard that
SKIPS. A lock component IS try-acquire-or-fail (skip on failure — the right shape, where an actor
queues), but the API is Alpha. Adopting it is an owner decision; until then notifications stays at
the chart's 1 replica, and nothing enforces that: the reconcile tick's overlap guard is a
process-local `asyncio.Lock`, so a second replica would re-walk the same rows (twice the FGA and actor
load, not a wrong answer).

## Sidecar map — who can call Dapr APIs at all

The control plane (injector, sentry, operator, placement, scheduler) is the platform's, a prerequisite installed once
per cluster (ADR 0153, [[XC-128]]); rask renders only its Components, Configurations and Resiliency, and refuses to
render for a cluster that does not serve `dapr.io/v1alpha1`. Development installs it from `deploy/fixtures/dapr` in
`dapr-system`, with `dapr_rbac.secretReader` off (`rask-helm` §8).

Sidecars are injected for the fleet + lakehouse services (`rask.daprAnnotations`; the
injector webhook is fail-closed via the paired label — a pod with the annotation but no label is the
silent no-sidecar failure). **No sidecar:** the 7 web zones, Ray head/workers, and every runner.
Anything running there gets its secrets by other means; the ESO path
(ESO, a prerequisite since XC-004) is the sanctioned k8s-native alternative when a pod must hold a secret and has no
sidecar.

## Traps already paid for (don't rediscover)

- **A Configuration's secret scope binds the app's API, never a component.** Dapr checks `secrets.scopes` only on
  `GET /v1.0/secrets/...` (`pkg/api/universal/secrets.go:35`); a component's `secretKeyRef` resolves through the store
  with the sidecar's own login (`pkg/runtime/processor/secret/secret.go:162-170`). So a secret only a component reads
  (a pub/sub user, the state store's DSN) goes in the app's STORE policy and in no `allowedSecrets`, and only the
  store's policy keeps a sidecar from a secret. Same in 1.18.1 and 1.19.0-rc.4.
- **`{appID}` is filled into ANY component's metadata** when the sidecar starts (`pkg/runtime/meta/meta.go:96-97`,
  documented as templated metadata values, with `{podName}`, `{namespace}`, `{uuid}`). That is how one `lance-secrets`
  logs every sidecar in as its own app's role.
- **A Vault store that logs in at init fails its sidecar's start closed** (1.19's Kubernetes auth): a missing role, a
  sealed or unreachable store, or a projected token the role's audience refuses keeps daprd from starting. A running
  sidecar renews in the background and logs in again once the store answers.
- An actor state store the app-id is not SCOPED to ⇒ sidecar logs "Workflow engine started" and
  actor hosting silently disables — healthy pod, permanently empty feature. Probe `/v1.0/metadata`
  for the ACTOR capability, not the log line.
- daprd APPENDS its caller-identity headers and FastAPI binds the FIRST duplicate, so a client-supplied
  `dapr-caller-app-id` is a claim, never an identity. No door authorizes on it: a Dapr-delivered route
  authenticates on `dapr-api-token` alone (`require_dapr_token`), which a request through the cluster
  route never carries because it reaches the app port without a sidecar, and a caller's identity is its
  verified bearer (ADR 0156 C5).
- A cron/input binding is delivered to `POST /<component-name>` at the pod ROOT — never under
  `RASK_API_PREFIX`. Component name, env var and served path are one string, pinned by invariants.
- **`bindings.cron` FIRES ON EVERY REPLICA.** The component is stateless and uncoordinated — Diagrid,
  verbatim: *"No coordination – each replica runs the schedule independently, causing duplicate
  triggers"* and *"when the target app is scaled to multiple replicas, the schedule will fire on every
  instance"*. There is no lease anywhere in the path, so every multi-replica service must buy the
  guarantee itself, and the estate answers it FIVE different ways: lineage's relay tick makes every step idempotent (an
  insert-only merge on each feed row, a forwarder cursor that only moves forward); notifications and maintenance are pinned to `replicas: 1`; catalog's
  control relay accepts duplicates because a duplicate publish dedupes on `event_id`; catalog's erasure
  release and promotion expiry move each record by an ETag-guarded write, so one replica wins it; compute's
  prune accepts them because the operation is convergent (`9489d5e1`: a job another replica had
  already reclaimed is not a retention FAILURE). A new cron on a service that can scale needs its answer chosen and written down, or its
  absence reads as an oversight rather than a decision.
- `ActorProxy` dispatches @actormethod WIRE names, not Python names; mocks cannot catch a mismatch.
- Missing `dapr.io/app-token-secret` on a bus-subscribing app ⇒ `assert_app_token_configured`
  crash-loops the pod. Missing row in `dapr-resiliency.yaml` ⇒ no sidecar retry AND no dead-letter.
- **A bus outage longer than about two minutes closes every `pubsub.jetstream` connection for good, and the pods
  stay Ready.** Dapr 1.18.1's component (components-contrib v1.18.0, `jetstream.go:60-77`) connects with nats.go's
  defaults, 60 reconnect attempts 2 s apart, and no metadata field changes them. After the last attempt the client
  closes: every subscription is dropped and every publish answers `nats: connection closed`. daprd's liveness is a TCP
  check and its readiness reads no component, so nothing restarts it, and with HotReload off it re-creates no component
  on its own. Measured on the kind lane 2026-10-17: after a 150 s outage, lineage's and notifications' durables
  acknowledged nothing for sixteen minutes. Each service with a jetstream component runs `service_kit.bus_watchdog`. It
  probes each component on `probe.<app-id>`, a subject no stream captures (one row per app in `nats.auth.users`), and
  asks the sidecar to shut down (`POST /v1.0/shutdown`, answered only on the pod's loopback) once the bus greets a
  connection again. It never does so sooner: a sidecar started while the bus refuses connections fails its component
  and exits. A new service with a jetstream component needs three things or it wedges on the first long outage: it
  starts the watchdog in its lifespan, it gets `lance.busEnv`, and it gets its probe row. Upstream:
  components-contrib#4563, fix #4564. Delete the watchdog once a Dapr release ships the fix.
- **A Resiliency retry whose `policy` and `duration` disagree is silently ineffective, and nothing
  rejects it.** dapr/kit's `NewBackOff` reads `Duration` ONLY under `PolicyConstant`; under
  `PolicyExponential` it is ignored and the schedule comes from `InitialInterval`/`Multiplier`/
  `RandomizationFactor` — none of which `resiliencies.dapr.io` declares, so the first step is pinned at
  cenkalti/backoff's 500ms default. `policy: exponential` + `duration: 30s` therefore renders, applies,
  and retries for **~4 seconds**. Measured 2026-09-14 by driving a poison delivery at `bronze-to-silver`
  and timing the park: 3.553s against a chart that documented 7.5 minutes. **`constant` + `duration` is
  the only shape that reaches minutes, and the only deterministic one** (`randomizationFactor` is
  equally unsettable, and exponential's 0.5 default swings the total ±50%). When sizing the window,
  spend it on FEW LONG steps: the handler's own time per attempt counts against the component's
  `ackWait`, so the same total split into many short steps overruns it and the broker redelivers
  underneath a retry still in flight. Gated by
  `tests/unit/test_the_pubsub_retry_window_is_the_one_the_chart_states.py`. **A Resiliency edit reaches
  a sidecar ONLY on restart**, exactly like a Component (`HotReload: false`): after `helm upgrade` the
  CR was correct in the API server while every running sidecar kept the old schedule, so rolling the
  chart is half the change — `kubectl rollout restart` every app-id in `targets.components`.
