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
| Bindings | ✅ | `bindings.cron` ×6 (sweep, reconcilers, prune), `bindings.smtp`, `bindings.http` — delivered to `POST /<component-name>` at the pod ROOT |
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
- **Jobs API** (2026-08-28): the six crons stay `bindings.cron` — operator-owned schedules in values,
  pinned by invariants (Component name = env var = served path, one string). **Revisit trigger:** the
  first genuine ONE-SHOT future job ("retry in 10m", "expire at T+24h") — bindings cannot express
  one-shots, the Scheduler control-plane already runs here (actor reminders use it), and Jobs is the
  idiomatic answer THEN. Mind its guarantee: at-least-once, durability over punctuality.

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
- `ActorProxy` dispatches @actormethod WIRE names, not Python names; mocks cannot catch a mismatch.
- Missing `dapr.io/app-token-secret` on a bus-subscribing app ⇒ `assert_app_token_configured`
  crash-loops the pod. Missing row in `dapr-resiliency.yaml` ⇒ no sidecar retry AND no dead-letter.
