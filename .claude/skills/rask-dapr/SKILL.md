---
name: rask-dapr
description: "Dapr in rask: which of the 12 building blocks this estate uses (3) and why, which it does not (9) with the recorded rulings, and the component-vs-DIY rubric — when a Dapr component is the answer and when hand-rolling is justified. Use when adding a capability that smells like queuing/scheduling/locking/config/secrets/middleware, when tempted to hand-roll one of those, when a Dapr API seems to fit a new feature, or when touching sidecar annotations, Components, or the dapr-resiliency map."
---

# rask × Dapr — what we use, what we refused, and how to decide

The generic sources do not answer rask's questions: `diagrid-labs/dapr-skills` is workflow+agent
AUTHORING (its own README title; its prereqs even assume Docker, which rask forbids), and the Dapr
docs list the 12 building blocks without tradeoffs. This skill is the estate-grounded layer: every
claim below was verified against the render or the code (2026-08-28), not inherited from docs.

**ADR 0199 makes NATS JetStream the lakehouse's one bus and plans the move off Dapr** (its own row after LH-494, an
ADR of its own). Until then this file describes the Dapr the estate runs; a new capability is not built on a Dapr
block that the move would have to undo.

## The inventory — 3 of 12 blocks in use

| Block | Used | Where (verified) |
| --- | --- | --- |
| Service invocation | ❌ | No service in the estate calls another through it, and the product chart renders no invocation Resiliency. Public traffic never rides invocation: the cluster route reaches each app port directly. A client that invokes a lakehouse app through its own sidecar renders its own policy, since Dapr applies an invocation policy on the CALLING sidecar |
| State | ❌ | No service in the estate keeps Dapr state, and the product chart renders no state store. Its one user, the notifications inbox, is retired (ADR 0193); a client that hosts actors brings its own store in its own release, scoped to its app-id (see traps) |
| Pub/sub | ✅ | `pubsub.jetstream`, one Component per app-id that publishes or subscribes, each scoped to ONE app-id and naming that app's NATS user from the store (`jwt`/`seedKey` secretKeyRef to `nats-user-<app>`, `auth.secretStore: lance-secrets`; the permission table is `nats.auth.users` in values.yaml, [[XC-078]]); `DaprApp.subscribe` incl. a DLQ route; `queueGroupName` makes replicas competing consumers. Unset, the component hands its app ONE message at a time (`concurrencyMode` defaults to single, components-contrib v1.19.0 `pubsub/jetstream/metadata.go:63-65`; measured live 2026-10-23, 61 of 61 lineage deliveries back to back): lineage's subscriber alone says `parallel`, bounded by its durable's `maxAckPending` (`services.lineage.deliveriesInFlight`), so JetStream hands out no more and nothing waits in the sidecar past its `ackWait`. A sidecar cannot rebind a durable whose config changed, so the NATS stream Job recreates `lineage-durable` at a new bound and checks again until a sidecar has recreated it at that value, since the outgoing pod's may recreate it as it was ([[LH-533]]) |
| Bindings | ✅ | `bindings.cron` ×7 in the product chart, six scoped to the catalog and one to lineage (maintenance runs none: its times are NATS message schedules on its own work queue, which fire once whatever its replica count, ADR 0198 decisions 5 and 6): `lineage-relay-cron` (drains the outbox, prunes the feed, forwards to the graph backend), `catalog-control-relay-cron`, `catalog-erasure-release-cron` (ADR 0138, releasing an erasure's pinning tags and branches when due), `catalog-promotion-expiry-cron` (ADR 0161, expiring promotion holds past their review deadline), `catalog-commit-reconcile-cron` ([[LH-497]], announcing each committed version lineage's feed lacks), `catalog-manifest-reclaim-cron` ([[LH-251]], ADR 0185, removing each root's `__manifest` versions past their retention), `catalog-declaration-converge-cron` ([[XC-213]], ADR 0206, converging the projects, warehouses and service grants the chart's `catalog.declared` values declare); `catalog-control-relay-cron` renders only with `catalog.controlEmit` and `catalog.controlOutbox.enabled`, and `lineage-relay-cron` only with `services.lineage.relay.enabled`. A client's release renders its own — read the cluster for what runs, not the templates. Each cron is an input binding delivered to `POST /<component-name>` at the pod ROOT. The product chart renders no output binding |
| Actors | ❌ | No service in the estate hosts an actor, so placement and the Scheduler hold nothing of the lakehouse's. The one host, the notifications inbox, is retired (ADR 0193) |
| Secrets | ✅ | `secretstores.hashicorp.vault` → the platform's Vault or OpenBao (the dev OpenBao fixture in development; the chart renders none, [[XC-149]]). ONE component, `lance-secrets`, logs each sidecar in with Kubernetes auth as the role `<release>-{appID}` (Dapr 1.19+), only to resolve its own components' `secretKeyRef`s (each pub/sub component's bus user). NO APP READS A SECRET THROUGH IT ([[XC-199]]): each service logs in to the store itself, as the same role, with the pod's projected token mounted into its own container (`lakehouse.governed.secrets`, env from `lance.secretStoreEnv`): before it serves, failing its start closed when the store gives it no usable login (one the store admits and then answers about itself, `auth/token/lookup-self`, which each rendered policy grants; a login refused that question is made again at the next attempt), and again once half its token's lease is used or on the pass that finds the store refusing that question (asked every 15 s; a refusal costs a login, and logins the store goes on refusing wait twice as many passes each time, fifteen at most), on a loop its supervisor keeps (`secret_store_session`, entered first in each lifespan). While that loop is passing no read logs in, on a refused secret or on a login that ran out: the start and the loop do (a read logs in itself only once the loop has made no pass for a minute), and the store answers 403 for a dropped token and for a secret the policy does not name alike. Each app's Configuration denies the sidecar's secrets API whole (`lance.storeReads`, ADR 0169 as amended 2026-11-14). THE estate rule: sole source, fail-closed, never env fallback. Every read is held at most a jittered 300-600 s and read again (`lakehouse.governed.secret_cache`, ADR 0166), so a rotation in the store reaches running pods; no lakehouse service holds an object-store key pair, each one's store identity being its ServiceAccount token (ADR 0170, ADR 0197), and the app-token door accepts the current or previous token |
| Workflow | ❌ | Nothing in this repository runs a workflow. The lakehouse runs none: the import contract `the-lakehouse-is-not-built-on-a-workflow-engine` forbids it (ADR 0154). The promotion gate is a tag, not a saga: readers read `@published`, approval moves the tag, rejection restores main, `catalog-promotion-expiry-cron` expires stale holds (ADR 0156 C1, ADR 0161, [[LH-466]]) |
| Configuration | ❌ | Ruled out — see refusals |
| Middleware | ❌ | Ruled out — see refusals |
| Distributed lock | ❌ | Not yet — the ONE open candidate (see below) |
| Jobs | ❌ | Cron bindings instead — revisit trigger recorded below |
| Crypto / Conversation | ❌ | No use case; Conversation would put an LLM key in a Component |

## The rubric — component vs DIY

**Reach for the Dapr component when ALL of these hold:**
1. **The surface has a sidecar.** Non-negotiable precondition — see the sidecar map below.
2. **The need is cross-cutting** (every service wants it the same way) rather than one route's semantics.
3. **Declarative Component YAML + a pod roll is an acceptable change cadence** — this estate runs
   `HotReload: false` (measured: ~540 ERROR lines/hour from a reconcile loop that cannot converge), so
   a Component edit reaches a sidecar only when its pod restarts; the chart rolls exactly the pods whose sidecar loads
   it (`checksum/dapr`, `lance.daprDigest`, [[XC-164]]), so a reconcile of git delivers it with no hand step.
4. **The block's guarantee matches the need as SPECIFIED, not approximately** — e.g. Jobs is
   "at-least-once, no ceiling on lateness"; a lock is acquire-or-fail. If you must wrap the block to
   correct its semantics, that wrapper IS the DIY and the block bought you a dependency.

**DIY is justified when ANY of these hold (and the justification is written at the site):**
- **No sidecar on that hop.** The two standing examples: the PUBLIC edge (the platform's Gateway →
  each service's app port is direct, so no Dapr middleware/ratelimit can ever see a client request — the body cap
  therefore lives in the app, `lakehouse.body_limit`, by owner ruling 2026-08-26, and no rask service rate-limits
  (no limiter is in the tree, measured 2026-11-07)), and
  **a bring-your-own job's pods** (no daprd, so no Dapr API to publish, subscribe or bind through).
- **Per-request semantics the block cannot express.** A reconcile guard that must SKIP an overlapping tick, not
  queue it, cannot be an actor (turn-based, so it queues); it is a process-local lock on a single replica until a
  cross-pod guard that skips exists (below).
- **The render is the gate.** rask's invariant tests read config out of `helm template`; state moved
  behind a runtime API (the Configuration block) is state those gates can no longer see.
- **The block is Alpha and the need is load-bearing.** Alpha components (lock, most middleware) may
  churn; a correctness-critical seam should not pivot on one without a recorded owner decision.

**Never DIY these, ever:** secret distribution (the secret store is the sole source, read by each service under
its own login through the one seam `lakehouse.governed.secrets` — no env fallback, no "graceful degradation"), pub/sub
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
- **Jobs API** (2026-08-28, re-judged 2026-11-09 against the code as it runs). The seven crons stay `bindings.cron`,
  and maintenance needs no scheduler from Dapr at all.

  **Maintenance's times are NATS message schedules on its own work queue** (ADR 0198 decisions 5 and 6,
  `maintenance/services/queue.py`). Each table's next pass is a schedule on `maintenance.schedule.<kind>.<uuid>` in
  `MAINTENANCE_SCHEDULES` that fires into `maintenance.due.<kind>.<uuid>`, a newer one replacing it; a commit holds the
  table's follow-up `maintenance.queue.commitPassDelaySeconds` (300) out; every table is passed at least every
  `maintenance.queue.recheckHours` (24); and the estate-wide work is two schedules, the audit
  (`maintenance.queue.auditSchedule`, `@hourly`: the drift report, its repairs and the lost-task backstop) and the scan
  (`maintenance.queue.scanSchedule`, `0 30 0 * * *`: the audit with the orphan scan, the listing floors and the trash
  purge). A schedule lives on the bus, fires once whatever the replica count, and the worker takes the fired task off a
  work queue that holds one per table, so it needs no lease, no lock and no cron binding.

  **Why the Jobs API still loses, on today's code:** a Dapr job would be a second home for times the bus already holds,
  scheduled through a sidecar whose publish cannot set the `Nats-Schedule` headers (`queue.py` cites
  `pubsub/jetstream/jetstream.go:115-145`), and invisible to the render gates. ADR 0199 makes NATS JetStream the one bus
  and plans the move off Dapr, so a new time-shaped need goes to a NATS message schedule, not to a Dapr block.

  **The audit of 2026-08-28** (10 time-shaped clusters, adversarial) found zero SHOULD_USE_JOBS, and the candidates it
  refuted stay refuted on today's code:
  * *auto-purge at trash deadline* → the daily scan's trash purge (`MAINTENANCE_TRASH_PURGE_ENABLED`, report-only by
    default, a recorded destruction posture). A purge tolerates a scan's lateness and must re-verify state at execution
    anyway (`purge.due_records` is the one shared rule).
  * *TTL grants* → already live as an FGA CEL condition (`non_expired_grant`, `packages/lakehouse/src/lakehouse/governed/auth/model.fga:741`) —
    check-time evaluation, strictly better than revoke-at-T (no window a crashed revoker leaves open).
  **The real revisit trigger:** a one-shot that is scoped to NOTHING that already holds state and that no NATS message
  schedule can carry. None exists.

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

**Distributed lock**, for a cron reconciler that must SKIP a tick another replica is running. A lock component IS
try-acquire-or-fail (skip on failure — the right shape, where an actor queues), but the API is Alpha, and no service in
the estate needs it today: maintenance's replicas share a work queue that holds one task per table (ADR 0198), and
lineage's and the catalog's crons are idempotent or ETag-guarded (traps below). Adopting it is an owner decision.

## Sidecar map — who can call Dapr APIs at all

The control plane (injector, sentry, operator, placement, scheduler) is the platform's, a prerequisite installed once
per cluster (ADR 0153, [[XC-128]]); rask renders only its Components, Configurations and Resiliency, and refuses to
render for a cluster that does not serve `dapr.io/v1alpha1`. Development installs it from `deploy/fixtures/dapr` in
`dapr-system`, with `dapr_rbac.secretReader` off (`rask-helm` §8).

Sidecars are injected for the lakehouse services (`rask.daprAnnotations`; the
injector webhook is fail-closed via the paired label — a pod with the annotation but no label is the
silent no-sidecar failure). **No sidecar:** the console's web pod and any bring-your-own job.
Anything running there gets its secrets by other means; the ESO path
(ESO, a prerequisite since XC-004) is the sanctioned k8s-native alternative when a pod must hold a secret and has no
sidecar.

## Traps already paid for (don't rediscover)

- **The lakehouse publishes its own CloudEvent, and three of the sidecar's behaviours decide its shape** ([[XC-199]],
  measured on daprd 1.19.0-rc.4 against nats-server 2.15.2, 2026-11-13). A body published as
  `application/cloudevents+json` is passed through as the envelope it is, and its `id` becomes the message's
  `Nats-Msg-Id`: the stream's duplicate window then drops a second publish of that id and still acknowledges it, so the
  id is per event, never per run (`lakehouse.cloud_events`; a run's START and COMPLETE share a run id). An envelope
  carrying `traceparent` and no `traceid` is stored with both blank and its subscriber's trace is all zeros, so the
  publish sets `traceid` equal to `traceparent` (`lakehouse.dapr_publish`). And only structured mode works: the
  component hands a subscriber no NATS header, so an envelope sent as headers reaches the app as bare data. A body
  published as `application/json` is wrapped under an id the sidecar mints per publish.
- **A Configuration's secret scope binds the app's API, never a component.** Dapr checks `secrets.scopes` only on
  `GET /v1.0/secrets/...` (`pkg/api/universal/secrets.go:35`); a component's `secretKeyRef` resolves through the store
  with the sidecar's own login (`pkg/runtime/processor/secret/secret.go:162-170`). Every product Configuration denies
  that API whole (`defaultAccess: deny` and no allow list, [[XC-199]]), which keeps an APP from its sidecar's store
  login and nothing more: a secret a component reads (a pub/sub user) goes in the app's STORE policy, and only the
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
- A cron/input binding is delivered to `POST /<component-name>` at the pod ROOT — never under an API
  prefix. Component name, env var and served path are one string, pinned by invariants.
- **`bindings.cron` FIRES ON EVERY REPLICA.** The component is stateless and uncoordinated — Diagrid,
  verbatim: *"No coordination – each replica runs the schedule independently, causing duplicate
  triggers"* and *"when the target app is scaled to multiple replicas, the schedule will fire on every
  instance"*. There is no lease anywhere in the path, so every multi-replica service must buy the
  guarantee itself, and the estate answers it three different ways: lineage's relay tick makes every step idempotent (an
  insert-only merge on each feed row, a forwarder cursor that only moves forward); catalog's
  control relay accepts duplicates because a duplicate publish dedupes on `event_id`; catalog's erasure
  release and promotion expiry move each record by an ETag-guarded write, so one replica wins it; catalog's declaration
  converge runs one tick at a time per process (an `asyncio.Lock`) and, across replicas, repeats the same idempotent
  writes (conditional registry records, a grant record created once, OpenFGA writes with duplicates ignored). A new cron on a service that can scale needs its answer chosen and written down, or its
  absence reads as an oversight rather than a decision.
- **A cron tick through the read-only window is answered 2xx and does nothing.** The chart's one `readOnlyWindow` holds
  every tick: the catalog's are held in `maintenance_mode` before routing, each one recorded by its own mount
  (`catalog.api.cron.ticks_of`; a tick mounted elsewhere than `mount_cron` calls `record_tick`, or it keeps writing
  through a window), lineage's relay returns `paused`. A 2xx, not a
  refusal: the cron component discards the handler's result (components-contrib `bindings/cron/cron.go`, `_, _ =
  handler(...)`), so a refusal would buy only daprd's error log, and the next tick after the window does the work.
- `ActorProxy` dispatches @actormethod WIRE names, not Python names; mocks cannot catch a mismatch.
- Missing `dapr.io/app-token-secret` on a bus-subscribing app ⇒ `assert_app_token_configured`
  crash-loops the pod. Missing row in `dapr-resiliency.yaml` ⇒ no sidecar retry AND no dead-letter.
- **A bus outage longer than about two minutes closes every `pubsub.jetstream` connection for good, and the pods
  stay Ready.** Dapr 1.18.1's component (components-contrib v1.18.0, `jetstream.go:60-77`) connects with nats.go's
  defaults, 60 reconnect attempts 2 s apart, and no metadata field changes them. After the last attempt the client
  closes: every subscription is dropped and every publish answers `nats: connection closed`. daprd's liveness is a TCP
  check and its readiness reads no component, so nothing restarts it, and with HotReload off it re-creates no component
  on its own. Measured on the kind lane 2026-10-17: after a 150 s outage, lineage's and notifications' durables
  acknowledged nothing for sixteen minutes. Each service with a jetstream component runs `lakehouse.bus_watchdog`. It
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
  a sidecar ONLY on restart**, exactly like a Component (`HotReload: false`), so the Resiliency is in every Dapr pod's
  `checksum/dapr` digest beside its Components: an edit rolls the pods whose sidecar loads it ([[XC-164]]). Every
  Component and the Resiliency live in `dapr-component.yaml` and `dapr-resiliency.yaml`, the two templates the digest
  reads (`test_event_signing_is_wired_by_the_chart_in_every_deploy_mode.py` refuses one rendered anywhere else).
