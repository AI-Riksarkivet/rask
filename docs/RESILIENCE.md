# Resilience & failure modes

What happens when a service goes down, can we get corrupted state, and how do we recover.

> **Honesty note on "chaos-tested".** The chaos table below was a **point-in-time** verification — each row
> was run once by pulling a live service on the kind cluster and observing recovery. It is **not** a standing
> regression suite, and the subscriptions now run on durable cursors (see the delivery-semantics callout below
> the table), so the lineage replay row stands for the `all` policy only. Treat this as "recovery was
> demonstrated once", not "continuously chaos-tested".
>
> **What IS a repeatable regression suite:** the `make e2e-*` targets run the live suites under
> `tests/e2e-py/` (observability, gateway, maintenance/compaction, CAS, governance boundary cases) against a
> deployed stack, and `make e2e-chaos` encodes the first chaos row below (scale lineage to 0, write, assert
> the replay). The other chaos rows are **not encoded as automated tests** — they mutate shared infra, so
> they stay out of the default lanes.

## The core guarantee: at-least-once + idempotent → no corruption, no loss (within bounds)

The event-driven path is `catalog --Dapr publish--> JetStream (LINEAGE / CATALOG_CONTROL streams) --> subscriber`.
Two properties make it safe:

1. **Durable buffer.** Both streams use **Limits retention** (messages persist after ack, up to `max-age`
   168h), so an event survives the subscriber being down.
2. **Idempotent ingest.** The graph **MERGEs on `run_id`**. So redelivery / replay of the same event is a
   **no-op** — never a duplicate, never corruption. The worst failure mode is *under-reporting* (a lost event), never a corrupt or doubled one.

## Chaos experiments (all recovered with zero loss)

| Experiment | What happened | Result |
| ---------- | ------------- | ------ |
| **Pull `lineage` (scale 0), publish 3 events, restart** | events buffered in JetStream (70→73); on restart the **ephemeral consumer replayed** them | **3/3 ingested** — no loss |
| **Kill `AGE` (Postgres) mid-ingest, restart** | ingest returned `RETRY`; Dapr redelivered per `backOff` until the DB was back | **ingested on redelivery** — no loss |
| **Idempotency** (replays re-deliver old events) | fixed `run_id`s re-MERGE | `gaptest1` has **exactly 1** producer run — no duplication |
| **Poison-inject 2026-09-14: a NON-CloudEvent body** (`rawPayload`) | the sidecar could not deserialize the envelope, dead-lettered it in **21ms with ZERO retries** (ahead of any retry policy), and the dead letter reached the DLQ topic as the same unreadable bytes | **parked INVISIBLY** — `/dlq-event` never ran, so no ERROR log and nothing for a dead-lettering alert to fire on. Tracked as LH-151 |

So: **a service going down delays the pipeline; it does not lose or corrupt data.** Transient dependency
failures recover via Dapr `RETRY`. With the DEFAULT `dapr.resiliency.enabled=true` the sidecar's Resiliency
policy owns the schedule — **constant 120s x 4 retries = 480s (8 min) over 5 attempts, no jitter**; a poison event
driven live 2026-09-14 measured 4 steps of 120.2/120.1/120.1/120.6s, **481.1s** end to end — and the broker ackWait is
720s crash-recovery only, so the two never race. The policy must be `constant`: the Resiliency CRD exposes no
`initialInterval`, so an `exponential` policy is pinned to cenkalti/backoff's 500ms default and gives ~4s however
long a `duration` sits beside it. With `resiliency=false` the broker owns it instead: ackWait 30s, maxDeliver 5,
backOff `30s,60s,120s,300s` — there the first backOff step IS the effective ack window per NATS consumer
semantics, so it must not undercut the slowest handler. Either way the window covers a realistic dependency blip.

> **Delivery semantics per subscriber.** Each subscriber has its **own** pubsub component with
> `queueGroupName=<app-id>` (replicas = competing consumers → single delivery per app) and a `durableName`, so
> the consumer cursor survives pod death and redeploys. `deliverPolicy` is **`all` for lineage** (replay into the
> idempotent MERGE is the durability story, and the first chaos row holds) and **`new`** for notifications and
> maintenance, whose durable cursor delivers an event published while they were down on recovery
> (`chart/templates/dapr-component.yaml`).

## Where it CAN bite — the real gaps (honest)

1. **The catalog outbox gap (the #1 weakness).** The catalog emits lineage **inline-awaited + best-effort**
   *after* the Lance write commits to S3 (`services/catalog/core/lineage_emit.py`) — awaited (not a
   BackgroundTasks fire-and-forget) so the event reaches the durable Dapr/JetStream transport before the
   response returns, but its errors are swallowed so a lineage outage never fails the write. If the catalog
   crashes (or its sidecar is down) **between the S3 write and the publish**, the data exists on storage but
   **the lineage event is lost** — the graph under-reports that write. No corruption, but a provenance hole.
   *Mitigation (shipped):* the **B4 storage→graph reconcile** back-fills exactly this loss mode — a Dapr-cron
   sweep reads on-disk Lance versions and stamps any write the graph is missing (`/datasets/{name}/reconcile`
   + `services.lineage.reconcile`). The **object-store outbox** (`service_kit.lakehouse.outbox`, opt-in via the
   catalog's `LANCE_LINEAGE_OUTBOX_URI` and the relay's `LINEAGE_OUTBOX_URI`) narrows the window to commit→stage for producers that stage the event before
   publishing — a crash between the write and the stage is still left to the back-fill. *Ops surface (#83):* the outbox
   backlog is now viewable + replayable on demand — `GET /admin/dlq` (the saturation snapshot + the governed
   at-risk events) and `POST /admin/dlq/{run_id}/replay` (re-ingest one event, re-publish it, then drop; same `can_write_data`
   authz as a fresh ingest, audited), surfaced in the frontend at `/dlq`. This is the manual twin of the
   reconcile relay's drain — an operator no longer waits for the next tick.

2. **No dead-letter queue; `maxDeliver=5` — FIXED 2026-07-12 (`dapr.resiliency.enabled`, DEFAULT ON;
   `false` = the exact pre-existing broker-only behavior).** The gap as it stood: a genuinely poison
   message (always `RETRY`, not malformed) was dropped from the *consumer* after 5 deliveries
   (~8.5 min of backOff) with **no DLQ**; limits retention kept it in the *stream*, so the lineage
   consumer (`deliverPolicy: all`) re-saw it on restart — but an outage longer than the retry window
   meant the event wasn't ingested **until a restart**. The fix is the Dapr-native SET (only correct
   together — a `deadLetterTopic` without a retry policy dead-letters on the FIRST failure, Dapr's
   documented default): a **Resiliency CRD** makes the sidecar own delivery retries (constant
   120s x 4 retries = 480s over 5 attempts, deterministic — the old broker schedule's shape, reached the
   only way the CRD can express it), every subscription declares a per-app
   **`deadLetterTopic`** (`dlq.*`, parked on the dedicated **DLQ stream** — load-bearing: Dapr does
   not auto-create streams, so without it the parking publish itself would fail), exhausted
   deliveries PARK there — ERROR-logged (`dapr_dead_letter_parked`) by each app's `/dlq-event`
   route, acked, never blind-requeued — and the broker `backOff` moves to a 720s crash-recovery
   window so it can't race the sidecar's retries. The durable-consumer question was ANSWERED FROM
   SOURCE (components-contrib `jetstream.go` applies `durableName` as-is per subscription; NATS
   scopes consumer names PER STREAM; the dlq topics live on their own stream). CI render-asserts the set
   ships together and the escape hatch restores the chaos-verified schedule. **DRIVEN LIVE 2026-09-14**
   (see the chaos table above, and [RUNBOOK-oncall § DLQ parking](runbooks/RUNBOOK-oncall.md#dlq-parking--a-delivery-gave-up)):
   a poison event retried, exhausted the schedule and parked on its `dlq.*` topic, ERROR-logged by
   `/dlq-event` — measured at **481.1s** end to end on helm revision 156, against the 480s the policy
   specifies. **A policy change reaches a sidecar only on
   restart** (`HotReload: false`), so rolling the chart is not enough: every subscriber app-id was
   rollout-restarted before this was re-measured. The once-planned **durable PULL consumer** is RETIRED (2026-07-12): PULL means consuming NATS directly (nats-py), i.e. leaving Dapr pub/sub — which contradicts the pinned Dapr-first rule — and its target gaps (cursor loss, silent exhaustion) are since covered by durable push cursors + sidecar Resiliency retries + this DLQ. Revisit only if a live delivery-semantics gap appears that Dapr's model cannot express.

3. **Durable cursors; lineage full-stream-replay by design.** The `new` subscribers pair `deliverPolicy: new`
   with a `durableName` (`chart/templates/dapr-component.yaml`): the consumer cursor survives pod death and
   redeploys, so an event published while a subscriber is down is **delivered on recovery** instead of skipped —
   chaos-verified live 2026-07-06 (publish-while-scaled-to-0 → `Unprocessed: 1` retained → processed on scale-up;
   post-redeploy consumption clean). Lineage is durable too, with `deliverPolicy: all` ([[LH-303]]):
   an ephemeral consumer dies with a NATS restart and the sidecar does not re-create it (measured
   2026-09-26: lineage heard nothing from the bus until its pod restarted). Its first attach replays the
   retained stream into the idempotent ingest; to REBUILD the graph from the stream, remove the durable
   and restart lineage — `nats consumer rm LINEAGE lineage-durable`, then
   `kubectl rollout restart deploy/rask-lineage` — and the new durable replays from the first message. With NATS auth on, run it as `admin` ([OPERATORS.md](OPERATORS.md) § 7): nats-box itself runs as the read-only `monitor`.
   Residual: gap #2's poison/no-DLQ window, which durable cursors do not change; and the catalog's own
   broadcast consumer, still ephemeral by design (one per replica), which a NATS restart also removes.

4. **Best-effort durable feed.** The `/events` feed table write is best-effort (logged on failure); the
   AGE graph is authoritative. The feed can lag the graph — visibility, not correctness.

5. **Single points of failure (single-node infra).** Every stateful backing store runs as **one replica** on
   kind — this is a **demo topology, not an HA one**. If any of these pods dies, the pipeline stalls until it
   restarts (no data loss — PVCs persist — but no availability either):
   - **AGE / Postgres** — one StatefulSet pod; the whole lineage graph + the durable `/events` + `/runs` fold
     live here. Down → all ingest returns `RETRY` (buffered) and every lineage read fails.
   - **NATS / JetStream** — one node, `streamReplicas=1`; the Dapr pub/sub backbone. Down → no event flows at
     all (catalog emits swallow, notifications and maintenance hear nothing).
   - **The object store** (the MinIO fixture in development) — the S3 data plane. Down → no Lance read/write.
   - **OpenBao / OpenFGA / Dex** — one pod each; down → (respectively) app-tier secrets fail-closed at boot,
     authz checks fail, OIDC login fails.
   *Fix (prod):* multi-replica + `streamReplicas=3` for NATS, a real Postgres HA (Patroni/CNPG), a replicated
   object store. Tracked as infra work; **the app tier is already `replicas`-ready** (stateless, PDBs shipped).

6. **Lineage lifecycle-emit gaps (deferred, see `docs/adr/0007-7a-live-verification-residuals.md`).** Overwrite leaves stale column nodes on
   the reused id; reconcile false-flags a *deliberately* dropped table as `MISSING_ON_STORAGE` (stale
   `source_uri`). Both are provenance-visibility issues, not corruption. **deregister** now emits a lineage
   marker (was silent — fixed + live-verified). **rename** is unsupported on the `dir` backend (501), so it
   emits nothing — moot, not a gap.

7. **Durable-consumer config drift wedges every durable subscription at upgrade — self-heals in
   ~20–25 min (observed live 2026-07-13).** JetStream durables are create-once: upgrading a
   deployed stack whose `<app>-durable` consumers were created under a **different** consumer
   config leaves the new sidecars unable to bind them — they retry-loop
   `nats: consumer name already in use: creating consumer "<app>-durable" on stream "<STREAM>"`
   while the pods stay **Ready** and **nothing is delivered** (a silent outage — the probes are
   process-level, not delivery-level). Concrete trigger: the resiliency+DLQ default-ON change
   (maxDeliver 5→3, backOff `30s,60s,120s,300s` → `720s,720s`) — durables created 2026-07-06 under
   the old config blocked every `new` subscriber after the image roll. It **self-heals**:
   JetStream reaps the old unbound durables at their `inactive_threshold` (~20–25 min observed —
   rollout 06:53–07:00 UTC → reaped + recreated 07:19–07:20), the sidecars recreate them with the
   new config, and delivery resumes with **no manual intervention**. *Fast cutover (operators):*
   right after the rollout, `nats consumer rm` the `<app>-durable` consumers on
   LINEAGE/CATALOG_CONTROL/MAINTENANCE_WORK/MAINTENANCE_INDEX/DLQ — the sidecars recreate them within seconds. With NATS auth on, run it as `admin` ([OPERATORS.md](OPERATORS.md) § 7): nats-box itself runs as the read-only `monitor`. Fresh installs and
   CI never hit this (the consumers are created right the first time); only config-changing
   upgrades of an already-deployed stack do. **Shipped mitigation:** the stream-provision Job now
   reconciles drift on every `helm upgrade` — it deletes any `*-durable` whose
   maxDeliver/backOff fingerprint differs from the render's expected config (templated from the
   SAME values conditional as the pubsub components), and the sidecars recreate it within seconds;
   the ~25 min window applies only when upgrading with a chart older than that Job.

## Operational hardening (k8s posture)

Cloud-native guards on the *deployment*, complementing the event-path guarantees above. All applied via the
chart and live-verified on kind.

- **No plaintext secrets in the render.** OpenFGA's datastore DSN comes from `secretKeyRef`
  (`datastore.existingSecret`), not a hardcoded `postgres://lance:lance@…`; the S3 secret + AGE password are
  consumed from OpenBao at boot when a secret store is configured. `helm template -f values-prod.yaml` shows
  zero plaintext secret values in env/args.
- **Graceful rollout drain.** Every app Deployment has a `preStop` sleep + `terminationGracePeriodSeconds`
  (maintenance gets a longer budget for an in-flight sweep) so endpoint removal propagates before SIGTERM —
  in-flight requests drain instead of connection-refusing (`lance.preStop`, `lifecycle.*`).
- **Bounded Dapr publishes.** Every `publish_event` is wrapped in `asyncio.timeout`
  (`service_kit/dapr_publish.py`) so a wedged sidecar/NATS can't hang a write handler past its ack window.
- **Liveness + readiness probes on every workload.** App pods get an HTTP `readinessProbe` (`/readyz`,
  dependency-aware) **and** `livenessProbe` (`/livez`, process-up only — never restarts on a slow backend);
  the zones get `tcpSocket` probes (`lance.appProbes` / `lance.tcpProbes`).
- **Restricted container securityContext on every app pod.** `runAsNonRoot` (images ship a numeric
  `USER` — uid 10001 — so the kubelet can verify non-root at admission), drop **ALL**
  capabilities, no privilege escalation, `seccompProfile: RuntimeDefault`, and `readOnlyRootFilesystem`
  with a writable `/tmp` emptyDir for scratch (`lance.securityContext`, `security.readOnlyRootFilesystem`).
- **Single-flight reconcile.** The B4 reconcile sweep runs under a Postgres
  advisory lock (`repository.reconcile_lock`) so the per-replica cron can't double-drive a back-fill. And a
  **UNIQUE index on every AGE vertex label's MERGE key** (`ensure_graph_constraints` at boot + the age-init
  SQL) makes AGE's MATCH-then-CREATE safe under concurrency — a racing duplicate `CREATE` is rejected by the
  DB (`duplicate key value violates unique constraint "lineage_run_uniq"`) instead of leaving a dup vertex.
  Ingest MERGEs datasets in a **deterministic name-sorted order** so two concurrent ingests that first-create
  the same vertices acquire the index locks in the same order — no deadlock (the index's only new failure mode).

## Invariants of the lakehouse's own background work

The lakehouse runs no batch pipeline ([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)); a
pipeline's own invariants are its engine's business. Two rules bind the work the lakehouse does run, and each has a
test that fails without it:

- **Refuse new work while draining.** `service_kit.draining.retry_when_draining` guards every sidecar-delivered work
  door (notifications' subscriptions, maintenance's arrival, work and index routes): a draining process answers
  RETRY so the broker redelivers to a live replica. It answers RETRY, not DROP, because on a subscription that
  declares a `deadLetterTopic` daprd routes a DROP to that topic, so a good message would be parked for a human to
  find (`service_kit/draining.py` carries the daprd source references). Pinned by
  `packages/service-kit/tests/test_draining_refuses_admission.py`.
- **Randomise iteration order.** The maintenance sweep shuffles its dataset list and its failure-retry list
  (`maintenance/services/sweep.py`), so a dataset that fails every pass cannot starve the ones listed after it.
  Deleting the shuffle passes every functional test, which is why
  `services/maintenance/tests/test_the_planner_does_not_hand_out_datasets_in_listing_order.py` (the dataset list) and
  `tests/unit/test_maintenance_lineage.py::test_emit_sweep_lineage_caps_fail_emits_and_logs_the_drop` (the capped
  failure list) pin it.

## Prod hardening backlog (delegate-to-platform — NOT dev-blocking)

The 2026-07-05 don't-reinvent audit confirmed we **reinvent nothing k8s/Dapr owns** (zero code to delete), but
several **native switches are off** — deliberately deferred because they are prod-only (kind's default CNI
ignores NetworkPolicy) and footgun-sequenced. Full list + fix order in [`docs/adr/0009-12-prod-hardening-backlog-native-switches-off.md`](adr/0009-12-prod-hardening-backlog-native-switches-off.md) §12:
the big one is **L3 network** (no default-deny; the OpenBao secret store is reachable by any pod today),
then **least-privilege ServiceAccounts** (~13 pods on `default` with a mountable API token), **infra-pod
securityContext** (app tier hardened, infra not), and **Pod Security Admission** enforcement. Do not rush
these into the dev baseline — default-deny egress without a kube-dns allow bricks the cluster, and restricted
PSA would reject `lineage`/`openfga-migrate` until their root init containers are hardened.

## Bottom line

- **Corruption:** not possible on the event path — idempotent MERGE on `run_id`.
- **Loss:** only in the commit->stage window the outbox narrows the gap to (a crash between the write and
  the stage, or a stage that fails AND a publish that fails). Everything staged or successfully published is
  durable + replayed.
- **Recovery:** automatic — `RETRY` for transient faults, JetStream buffer + replay for downed services.
- **Hardening roadmap (for prod):** durable push cursors + Resiliency retries + Dapr `deadLetterTopic`
  (shipped, default on) · a transactional outbox that closes the commit→stage window (the remaining item).
