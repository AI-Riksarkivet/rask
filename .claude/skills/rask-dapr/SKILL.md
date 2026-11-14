---
name: rask-dapr
description: "Dapr in rask: which of the 12 building blocks this estate uses (1) and why, which it does not (11) with the recorded rulings, and the component-vs-DIY rubric — when a Dapr component is the answer and when hand-rolling is justified. Use when adding a capability that smells like queuing/scheduling/locking/config/secrets/middleware, when tempted to hand-roll one of those, when a Dapr API seems to fit a new feature, when adding a periodic tick to a service, or when touching sidecar annotations or Components."
---

# rask × Dapr — what we use, what we refused, and how to decide

The generic sources do not answer rask's questions: `diagrid-labs/dapr-skills` is workflow+agent
AUTHORING (its own README title; its prereqs even assume Docker, which rask forbids), and the Dapr
docs list the 12 building blocks without tradeoffs. This skill is the estate-grounded layer: every
claim below was verified against the render or the code (2026-08-28), not inherited from docs.

**ADR 0199 makes NATS JetStream the lakehouse's one bus and plans the move off Dapr** ([[XC-199]], #787, an ADR of its
own at its last step). Every event is published and taken on the service's own connection to the bus, and every
periodic tick is a fixed timer in the service's own process (`lakehouse.timers`); the catalog's and lineage's pods run a
sidecar that logs in to the secret store and delivers nothing to its app. This file describes that Dapr; a new
capability is not built on a Dapr block that the move would have to undo.

## The inventory — 1 of 12 blocks in use

| Block | Used | Where (verified) |
| --- | --- | --- |
| Service invocation | ❌ | No service in the estate calls another through it, and the product chart renders no invocation Resiliency. Public traffic never rides invocation: the cluster route reaches each app port directly. A client that invokes a lakehouse app through its own sidecar renders its own policy, since Dapr applies an invocation policy on the CALLING sidecar |
| State | ❌ | No service in the estate keeps Dapr state, and the product chart renders no state store. Its one user, the notifications inbox, is retired (ADR 0193); a client that hosts actors brings its own store in its own release, scoped to its app-id (see traps) |
| Pub/sub | ❌ | No service publishes or subscribes through it, and the product chart renders no `pubsub.jetstream` Component and no Resiliency. Each service publishes and takes its deliveries on its own connection to NATS JetStream as its own bus user (`lakehouse.bus`; the maintenance worker's queue, `maintenance.services.queue`; the permission table is `nats.auth.users` in values.yaml, [[XC-078]]): lineage and the worker pull durable consumers the chart's stream Job declares and create none, and each catalog replica follows CATALOG_CONTROL by reading it by sequence. A client that subscribes through its own sidecar renders its own Component, in its own release (see traps) |
| Bindings | ❌ | The product chart renders no binding. Every periodic tick of the catalog (six) and of lineage (its relay) is a fixed timer: a loop in each replica that claims each window with one conditional write of `_timers/<timer>.json` under the control root, so the tick runs once a window whatever the replica count (`lakehouse.timers`). Maintenance's times are NATS message schedules on its own work queue (ADR 0198 decisions 5 and 6). A client's release renders its own — read the cluster for what runs, not the templates |
| Actors | ❌ | No service in the estate hosts an actor, so placement and the Scheduler hold nothing of the lakehouse's. The one host, the notifications inbox, is retired (ADR 0193) |
| Secrets | ✅ | `secretstores.hashicorp.vault` → the platform's Vault or OpenBao (the dev OpenBao fixture in development; the chart renders none, [[XC-149]]). ONE component, `lance-secrets`, scoped to the catalog's and lineage's sidecars and to any `openbao.externalReaders` app (`lance.sidecarScopes`), logs each sidecar in with Kubernetes auth as the role `<release>-{appID}` (Dapr 1.19+). No product Component names a `secretKeyRef`, so it resolves no secret for the lakehouse; a client's sidecar may resolve its own components' secrets through it. NO APP READS A SECRET THROUGH IT ([[XC-199]]): each service logs in to the store itself, as the same role, with the pod's projected token mounted into its own container (`lakehouse.governed.secrets`, env from `lance.secretStoreEnv`): before it serves, failing its start closed when the store gives it no usable login (one the store admits and then answers about itself, `auth/token/lookup-self`, which each rendered policy grants; a login refused that question is made again at the next attempt), and again once half its token's lease is used or on the pass that finds the store refusing that question (asked every 15 s; a refusal costs a login, and logins the store goes on refusing wait twice as many passes each time, fifteen at most), on a loop its supervisor keeps (`secret_store_session`, entered first in each lifespan). While that loop is passing no read logs in, on a refused secret or on a login that ran out: the start and the loop do (a read logs in itself only once the loop has made no pass for a minute), and the store answers 403 for a dropped token and for a secret the policy does not name alike. Each app's Configuration denies the sidecar's secrets API whole (`lance.storeReads`, ADR 0169 as amended 2026-11-14). THE estate rule: sole source, fail-closed, never env fallback. Each holder reads its secret again on its own period (the signing key and the commit-id key every 300 s, a bus credential at each connect), so a rotation in the store reaches running pods; no lakehouse service holds an object-store key pair, each one's store identity being its ServiceAccount token (ADR 0170, ADR 0197) |
| Workflow | ❌ | Nothing in this repository runs a workflow. The lakehouse runs none: the import contract `the-lakehouse-is-not-built-on-a-workflow-engine` forbids it (ADR 0154). The promotion gate is a tag, not a saga: readers read `@published`, approval moves the tag, rejection restores main, the catalog's `catalog-promotion-expiry` timer expires stale holds (ADR 0156 C1, ADR 0161, [[LH-466]]) |
| Configuration | ❌ | Ruled out — see refusals |
| Middleware | ❌ | Ruled out — see refusals |
| Distributed lock | ❌ | Not yet — the ONE open candidate (see below) |
| Jobs | ❌ | Fixed timers and NATS message schedules instead — revisit trigger recorded below |
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
- **Per-request semantics the block cannot express.** A periodic tick that must run once a window across replicas,
  skipping a window another replica took rather than queueing behind it, cannot be an actor (turn-based, so it queues);
  it is a fixed timer whose window one conditional write on the store claims (`lakehouse.timers`), as Lance picks one
  writer of a version (`lance_docs/lance/format/table/transaction.md` § Storage Primitives).
- **The render is the gate.** rask's invariant tests read config out of `helm template`; state moved
  behind a runtime API (the Configuration block) is state those gates can no longer see.
- **The block is Alpha and the need is load-bearing.** Alpha components (lock, most middleware) may
  churn; a correctness-critical seam should not pivot on one without a recorded owner decision.

**Never DIY these, ever:** secret distribution (the secret store is the sole source, read by each service under
its own login through the one seam `lakehouse.governed.secrets` — no env fallback, no "graceful degradation"), bus
delivery and retry (the bus seam's: `lakehouse.bus.Bus.consume` pulls and settles each delivery by the one rule
`lakehouse.bus_delivery.judged`; a publish the bus did not take stays staged in its outbox for its relay, never a hand
retry loop around either), and actor placement/turn semantics.

## Recorded refusals (do not re-litigate without new facts)

- **HTTP middleware pipeline** (2026-08-28, three independent reasons): public traffic never traverses
  daprd (the cluster route reaches the app port); `middleware.http.ratelimit` is per-sidecar and keyed
  on remote IP, which on a service-invocation hop is the calling pod, never the client; one
  `dapr.io/config` per sidecar + HotReload-off makes a pipeline fleet-global and rollout-gated.
- **Configuration API** (2026-08-28): the fleet's config is ONE ConfigMap via `envFrom`, validated by
  pydantic-settings at boot, and the render-driven invariant tests depend on seeing it; change
  cadence is deliberately rollout-shaped. It solves a problem this estate chose not to have.
- **Jobs API** (2026-08-28, re-judged 2026-11-09 against the code as it runs). The catalog's and lineage's seven
  ticks are fixed timers in their own processes, and maintenance needs no scheduler from Dapr at all.

  **A fixed estate tick is a store-claimed timer** (`lakehouse.timers`, [[XC-199]]). Every replica runs the loop; once a
  window each reads the timer's claim record (`_timers/<timer>.json`, control prefix `TIMERS`, a lease-class prefix a
  control-root backup skips) and tries one conditional write that claims it, create-if-absent or replace under the ETag
  it read. The store lets one land and that replica runs the tick. A window is named by when it began on the STORE's
  clock (the read's `Date`), so no two pods' clocks are compared and a period changed in git is due at its own next
  boundary. A timer depends on nothing its work does not need: all seven need only the object store and the catalog,
  and lineage's relay and the control relay exist for the bus being away, so none is a schedule on the bus.

  **Maintenance's times are NATS message schedules on its own work queue** (ADR 0198 decisions 5 and 6,
  `maintenance/services/queue.py`). Each table's next pass is a schedule on `maintenance.schedule.<kind>.<uuid>` in
  `MAINTENANCE_SCHEDULES` that fires into `maintenance.due.<kind>.<uuid>`, a newer one replacing it; a commit holds the
  table's follow-up `maintenance.queue.commitPassDelaySeconds` (300) out; every table is passed at least every
  `maintenance.queue.recheckHours` (24); and the estate-wide work is two schedules, the audit
  (`maintenance.queue.auditSchedule`, `@hourly`: the drift report, its repairs and the lost-task backstop) and the scan
  (`maintenance.queue.scanSchedule`, `0 30 0 * * *`: the audit with the orphan scan, the listing floors and the trash
  purge). A schedule lives on the bus, fires once whatever the replica count, and the worker takes the fired task off a
  work queue that holds one per table, so it needs no lease and no lock.

  **Why the Jobs API still loses, on today's code:** a Dapr job would be a second home for times the bus already holds,
  scheduled through a sidecar whose publish cannot set the `Nats-Schedule` headers (ADR 0198 decision 5 cites
  `pubsub/jetstream/jetstream.go:115-145`), and invisible to the render gates. ADR 0199 makes NATS JetStream the one bus
  and plans the move off Dapr, so a new time-shaped need is never a Dapr block: a fixed estate-wide tick is a
  store-claimed timer on `lakehouse.timers`, and a per-object time (a table's next pass) is a NATS message schedule.

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
| recurring scan-and-converge on a fixed period | **fixed timer** (`lakehouse.timers`) | as ruled above: one store-claimed window per period, needing only the store |
| a time per object (a table's next pass) | **NATS message schedule** | ADR 0198 decisions 5 and 6: the bus holds it and fires it once |

Two failure smells, one per direction: a workflow whose body is ONLY `create_timer` → should be
Jobs; Jobs callbacks that CHAIN (A schedules B schedules C, payload accumulating state) → a
hand-built workflow without history, replay or per-step retry — should be Workflow.

## The one open candidate

**Distributed lock**, for work that must SKIP while another replica runs it. A lock component IS try-acquire-or-fail
(skip on failure — the right shape, where an actor queues), but the API is Alpha, and no service in the estate needs it
today: maintenance's replicas share a work queue that holds one task per table (ADR 0198), each of the catalog's and
lineage's timers claims its window on the store, and a tick that outlasts its window and overlaps the next one's is
idempotent or ETag-guarded (traps below). Adopting it is an owner decision.

## Sidecar map — who can call Dapr APIs at all

The control plane (injector, sentry, operator, placement, scheduler) is the platform's, a prerequisite installed once
per cluster (ADR 0153, [[XC-128]]); rask renders only its Components and Configurations, and refuses to render for a
cluster that does not serve `dapr.io/v1alpha1`. Development installs it from `deploy/fixtures/dapr` in `dapr-system`,
with `dapr_rbac.secretReader` off (`rask-helm` §8).

Sidecars are injected into the catalog's and lineage's pods, where they log in to the secret store and deliver nothing
to the app (`rask.daprAnnotations`; the
injector webhook is fail-closed via the paired label — a pod with the annotation but no label is the
silent no-sidecar failure). **No sidecar:** the maintenance worker, the console's web pod and any bring-your-own job.
A lakehouse service reads its secrets under its own store login whether or not its pod runs a sidecar
(`lakehouse.governed.secrets`); ESO (a prerequisite since XC-004) is the k8s-native path when a pod must hold a secret
as a Kubernetes Secret.

## Traps already paid for (don't rediscover)

- **The lakehouse publishes its own CloudEvent on its own connection, and a client's subscribing sidecar takes it as it
  is** ([[XC-199]], measured on daprd 1.19.0-rc.4 against nats-server 2.15.2, 2026-11-13 and 2026-11-14). The seam
  (`lakehouse.bus`) sends the envelope as the message body under `application/cloudevents+json`, with its `id` as the
  message's `Nats-Msg-Id`: the stream's duplicate window drops a second publish of that id and still acknowledges it,
  so the id is per event, never per run (`lakehouse.cloud_events`; a run's START and COMPLETE share a run id), and the
  seam believes a duplicate acknowledgement only after reading the stored message back. The envelope carries the
  publish's `traceparent` (and `tracestate`): the lakehouse's own lanes handle each delivery as a consumer span
  `process <subject>` under that trace, and a sidecar delivers it to its app under that trace too, in the request's
  `traceparent` header and in the envelope alike. Only structured mode works through a sidecar: the component hands a
  subscriber no NATS header, so an envelope sent as headers reaches the app as bare data.
- **A Configuration's secret scope binds the app's API, never a component.** Dapr checks `secrets.scopes` only on
  `GET /v1.0/secrets/...` (`pkg/api/universal/secrets.go:35`); a component's `secretKeyRef` resolves through the store
  with the sidecar's own login (`pkg/runtime/processor/secret/secret.go:162-170`). Every product Configuration denies
  that API whole (`defaultAccess: deny` and no allow list, [[XC-199]]), which keeps an APP from its sidecar's store
  login and nothing more: a secret a component reads (a client's pub/sub user, say) goes in that app's STORE policy, and
  only the store's policy keeps a sidecar from a secret. Same in 1.18.1 and 1.19.0-rc.4.
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
  `dapr-caller-app-id` is a claim, never an identity. No door authorizes on it: a caller's identity is its verified
  bearer (ADR 0156 C5).
- **A Dapr `bindings.cron` fires on every replica**, uncoordinated (Diagrid, verbatim: *"No coordination – each replica
  runs the schedule independently, causing duplicate triggers"*), and its component discards the handler's answer
  (components-contrib `bindings/cron/cron.go`, `_, _ = handler(...)`). That is why an estate tick is a store-claimed
  timer: the claim gives once a window, and a tick that outlasts its window and overlaps the next one's still has its
  own answer. Lineage's relay makes every step idempotent (an insert-only merge on each feed row, a forwarder cursor
  that only moves forward); the control relay accepts duplicates because a duplicate publish dedupes on `event_id`; the
  erasure release and the promotion expiry move each record by an ETag-guarded write, so one replica wins it; the
  declaration converge repeats the same idempotent writes (conditional registry records, a grant record created once,
  OpenFGA writes with duplicates ignored). A new timer needs its overlap answer chosen and written down at its site.
- **A timer through the read-only window is held.** The chart's one `readOnlyWindow` holds every timer of the catalog
  and lineage: a held turn claims nothing and runs nothing (`timer_held`, outcome `held`), and the first window after
  the window ends does the work. `LakehouseTimerStopped` does not page on a held timer.
- `ActorProxy` dispatches @actormethod WIRE names, not Python names; mocks cannot catch a mismatch.
- **A Component edit reaches a sidecar ONLY on restart** (`HotReload: false`), so every Dapr pod carries a
  `checksum/dapr` digest of the Components its sidecar loads (`lance.daprDigest`): an edit rolls the pods whose sidecar
  loads it ([[XC-164]]). Every product Component lives in `dapr-component.yaml`, the one template the digest reads.
- **A bus outage longer than about two minutes closes a `pubsub.jetstream` component's connection for good, and the
  pod stays Ready.** Dapr 1.18.1's component (components-contrib v1.18.0, `jetstream.go:60-77`) connects with nats.go's
  defaults, 60 reconnect attempts 2 s apart, and no metadata field changes them. After the last attempt the client
  closes: every subscription is dropped, and a publish through the sidecar answers `nats: connection closed`. daprd's
  liveness is a TCP check and its readiness reads no component, so nothing restarts it, and with HotReload off it
  re-creates no component on its own. Measured on the kind lane 2026-10-17: after a 150 s outage, two subscribing
  sidecars' durables acknowledged nothing for sixteen minutes. The product chart renders no such component, since each
  service opens its own connection again in-process (`lakehouse.bus.Bus.keep_connected`); a client that subscribes
  through its own sidecar inherits the trap. Upstream: components-contrib#4563, fix #4564.
- **A Resiliency retry whose `policy` and `duration` disagree is silently ineffective, and nothing
  rejects it.** The product chart renders no Resiliency; this is for a client's own. dapr/kit's `NewBackOff` reads
  `Duration` ONLY under `PolicyConstant`; under `PolicyExponential` it is ignored and the schedule comes from
  `InitialInterval`/`Multiplier`/`RandomizationFactor` — none of which `resiliencies.dapr.io` declares, so the first
  step is pinned at cenkalti/backoff's 500ms default. `policy: exponential` + `duration: 30s` therefore renders,
  applies, and retries for **~4 seconds**. Measured 2026-09-14 by driving a poison delivery at a subscription and
  timing the park: 3.553s against a chart that documented 7.5 minutes. **`constant` + `duration` is the only shape
  that reaches minutes, and the only deterministic one** (`randomizationFactor` is equally unsettable, and
  exponential's 0.5 default swings the total ±50%). A Resiliency edit, like a Component's, reaches a sidecar only on
  restart.
