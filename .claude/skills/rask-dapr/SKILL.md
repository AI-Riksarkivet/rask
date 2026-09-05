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
| Service invocation | ✅ | gateway `_target_base` → `/v1.0/invoke/{app_id}/method` (direct-httpx fallback when Dapr off); `invoke/lineage`, `invoke/compute` |
| State | ✅ | `state.postgresql` ×1 (the actor state store; scopes are load-bearing — see traps) |
| Pub/sub | ✅ | `pubsub.jetstream` ×6; `DaprApp.subscribe` incl. a DLQ route; `queueGroupName` makes replicas competing consumers |
| Bindings | ✅ | `bindings.cron` ×7 — measured on the deployed estate 2026-09-05: `maintenance-cron` (the sweep), `maintenance-reconcile-cron`, `lineage-reconcile-cron`, `notifications-reconcile-cron`, `catalog-control-relay-cron`, `compute-prune-jobs-cron`, `medallion-cascade-lag-cron`. `ingest-cron` renders from the chart but is not enabled here, so the CHART count is 8 and the RUNNING count is 7 — read the cluster, not the templates. Plus `bindings.smtp`, `bindings.http` — all delivered to `POST /<component-name>` at the pod ROOT |
| Actors | ✅ | `DaprActor` ×17, `ActorProxy` ×28 (notifications inbox, project tasks) |
| Secrets | ✅ | `secretstores.hashicorp.vault` → OpenBao. THE estate rule: sole source, fail-closed, never env fallback |
| Workflow | ✅ | `DaprWorkflowClient` ×49, `WorkflowRuntime` ×16 (medallion cascade, flows, ingest) |
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
- **No sidecar on that hop.** The two standing examples: the PUBLIC edge (Ingress → gateway:8888 is
  app-port direct, so no Dapr middleware/ratelimit can ever see a client request — rate limiting and
  body caps therefore live in `service_kit.middleware` / slowapi, by owner ruling 2026-08-26), and
  **Ray pods** (no daprd; a job cannot call `/v1.0/secrets/*`).
- **Per-request semantics the block cannot express.** notifications' reconcile guard must SKIP an
  overlapping tick, not queue it — an actor (turn-based) queues, so the estate keeps a process-local
  `asyncio.Lock` and a single replica until a cross-pod guard that skips exists.
- **The render is the gate.** rask's invariant tests read config out of `helm template`; state moved
  behind a runtime API (the Configuration block) is state those gates can no longer see.
- **The block is Alpha and the need is load-bearing.** Alpha components (lock, most middleware) may
  churn; a correctness-critical seam should not pivot on one without a recorded owner decision.

**Never DIY these, ever:** secret distribution (OpenBao via the Dapr secret store is the sole source —
no env fallback, no "graceful degradation"; scope the Component to every consuming app-id), pub/sub
delivery/retry (JetStream + `dapr-resiliency.yaml`, never a hand retry loop around a publish), and
actor placement/turn semantics.

## Recorded refusals (do not re-litigate without new facts)

- **HTTP middleware pipeline** (2026-08-28, three independent reasons): public traffic never traverses
  daprd; `middleware.http.ratelimit` is per-sidecar and keyed on remote IP, which on the only hop it
  could see is the gateway pod for every request; one `dapr.io/config` per sidecar + HotReload-off
  makes a pipeline fleet-global and rollout-gated. The gateway→backend invoke hop is real but is the
  wrong place for authn (it would validate the gateway's outbound call, not the client's inbound one).
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

  **What was falsified, so nobody re-derives it from this file:** the medallion tiers CAN carry a
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

**Distributed lock** for the notifications reconciler. The values-prod note + the invariant test
record the exit condition: a cross-pod guard that SKIPS. A lock component IS try-acquire-or-fail
(skip on failure — the right shape, where an actor queues), but the API is Alpha. Adopting it is an
owner decision; until then notifications stays at 1 replica and the constraint is enforced by
`test_notifications_stays_single_replica_while_its_single_flight_lock_is_process_local`.

## Sidecar map — who can call Dapr APIs at all

Sidecars are injected for the fleet + lakehouse services and movers (`rask.daprAnnotations`; the
injector webhook is fail-closed via the paired label — a pod with the annotation but no label is the
silent no-sidecar failure). **No sidecar:** the 7 web zones, Ray head/workers, and every runner.
Anything running there gets its secrets by other means — today the Ray lane's are injected by the
submitting service (which DOES fetch them from the Dapr store first); the ESO path
(`externalSecrets`) is the sanctioned k8s-native alternative when a pod must hold a secret and has no
sidecar. The Jobs-API echo of `runtime_env` is exactly why "inject at submit" is under review.

## Traps already paid for (don't rediscover)

- An actor state store the app-id is not SCOPED to ⇒ sidecar logs "Workflow engine started" and
  actor hosting silently disables — healthy pod, permanently empty feature. Probe `/v1.0/metadata`
  for the ACTOR capability, not the log line.
- daprd APPENDS its caller-identity headers; FastAPI binds the FIRST duplicate ⇒ the gateway must
  strip client-supplied `dapr-*` headers at the edge (`_CLIENT_SPOOFABLE`) or every door's caller
  check reads the client's own claim.
- A cron/input binding is delivered to `POST /<component-name>` at the pod ROOT — never under
  `RASK_API_PREFIX`. Component name, env var and served path are one string, pinned by invariants.
- **`bindings.cron` FIRES ON EVERY REPLICA.** The component is stateless and uncoordinated — Diagrid,
  verbatim: *"No coordination – each replica runs the schedule independently, causing duplicate
  triggers"* and *"when the target app is scaled to multiple replicas, the schedule will fire on every
  instance"*. There is no lease anywhere in the path, so every multi-replica service must buy the
  guarantee itself, and the estate answers it FOUR different ways: lineage takes a Postgres advisory
  lock (`RECONCILE_LOCK_KEY`); notifications and maintenance are pinned to `replicas: 1`; catalog's
  control relay accepts duplicates because a duplicate publish dedupes on `event_id`; and compute's
  prune accepts them because the operation is convergent (`9489d5e1` — until then a job another
  replica had already reclaimed was miscounted as a retention FAILURE, one false alarm per job
  reclaimed). A new cron on a service that can scale needs its answer chosen and written down, or its
  absence reads as an oversight rather than a decision.
- `ActorProxy` dispatches @actormethod WIRE names, not Python names; mocks cannot catch a mismatch.
- Missing `dapr.io/app-token-secret` on a bus-subscribing app ⇒ `assert_app_token_configured`
  crash-loops the pod. Missing row in `dapr-resiliency.yaml` ⇒ no sidecar retry AND no dead-letter.
