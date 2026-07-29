# Resilience & failure modes

What happens when a service goes down, can we get corrupted state, and how do we recover.

> **Honesty note on "chaos-tested".** The chaos table below was a **point-in-time** verification — each row
> was run once by pulling a live service on the kind cluster and observing recovery. It is **not** a standing
> regression suite, and the **§2 bus fixes changed the delivery semantics after those runs** (see the ⚠️
> callout below the table) — rows 1 and 3 must be **re-verified on the next deploy**. Treat this as "recovery
> was demonstrated once under the pre-§2 config", not "continuously chaos-tested".
>
> **What IS a repeatable regression suite:** `make e2e` runs the core end-to-end suites (observability,
> medallion cascade, gateway, compaction, CAS) against the deployed stack, and `make e2e-governance` the
> **boundary cases** (malformed-bearer→401, non-owner rename/overwrite→403, verified create-lineage) on an
> auth-on stack (`make e2e-all` runs both). `make e2e-governed-union` drives the FULL flag union
> (auth + FGA + compute + quality ON, OpenBao off): the governed cascade allow-path, a live FGA-deny→DROP
> (gold validator tuple revoked → gold's run never lands → re-grant restores), a live quality-block (bad
> batch recorded `quality_passed=false` in lineage, never promoted), and the media lane under governance —
> deploy flags in `tests/e2e/test_governed_union_e2e.py`. The **chaos rows below** (pull-a-service →
> recover) are **not yet encoded as automated tests** — they still need a mutating harness that scales a
> pod and asserts recovery (a documented follow-up; they mutate shared infra, so they're kept out of the
> default `make e2e`).

## The core guarantee: at-least-once + idempotent → no corruption, no loss (within bounds)

The event-driven path is `catalog --Dapr publish--> JetStream (LINEAGE/MEDALLION streams) --> subscriber`.
Two properties make it safe:

1. **Durable buffer.** Both streams use **Limits retention** (messages persist after ack, up to `max-age`
   168h), so an event survives the subscriber being down.
2. **Idempotent ingest.** The graph **MERGEs on `run_id`**; the medallion run_id is derived from the
   trigger token. So redelivery / replay of the same event is a **no-op** — never a duplicate, never
   corruption. The worst failure mode is *under-reporting* (a lost event), never a corrupt or doubled one.

## Chaos experiments (all recovered with zero loss)

| Experiment | What happened | Result |
| ---------- | ------------- | ------ |
| **Pull `lineage` (scale 0), publish 3 events, restart** | events buffered in JetStream (70→73); on restart the **ephemeral consumer replayed** them | **3/3 ingested** — no loss |
| **Kill `AGE` (Postgres) mid-ingest, restart** | ingest returned `RETRY`; Dapr redelivered per `backOff` until the DB was back | **ingested on redelivery** — no loss |
| **Pull `bronze-to-silver` mover, fire `/produce`, restart** | cascade **stalled at bronze** (trigger buffered in the MEDALLION stream); restart replayed it | **cascade resumed to gold** |
| **Idempotency** (replays re-deliver old events) | fixed `run_id`s re-MERGE | `gaptest1` has **exactly 1** producer run — no duplication |

So: **a service going down delays the pipeline; it does not lose or corrupt data.** Transient dependency
failures recover via Dapr `RETRY` (with the DEFAULT `dapr.resiliency.enabled=true` the sidecar's Resiliency
policy owns the retry schedule and the broker ackWait is 720s crash-recovery only; the numbers below
describe the `resiliency=false` escape hatch: ackWait 30s, maxDeliver 5, backOff `30s,60s,120s,300s` — the first
backOff step IS the effective ack window per NATS consumer semantics, so it must not undercut the slowest
handler; the ~8.5 min total window covers a realistic dependency blip).

> ⚠️ **Semantics changed by the §2 bus fixes (re-verify live on next deploy).** Each subscriber now has
> its **own** pubsub component with `queueGroupName=<app-id>` (replicas = competing consumers → single
> delivery per app; scaling movers past 1 is now safe) and a split `deliverPolicy`: **`all` for lineage**
> (the restart-replay row above still holds — replay into the idempotent MERGE is the durability story)
> but **`new` for the cascade head + movers** — a full-stream replay there would *re-fire every cascade
> in the retention window* on each restart. Consequence: the third row changes — a trigger published
> while a mover is down **beyond the ephemeral consumer's inactivity window** is no longer replayed on
> restart (quick restarts rejoin the surviving queue-group consumer and keep its pending messages).
> **CLOSED 2026-07-06:** the `new` subscribers (cascade head + movers) now carry a `durableName`, so the
> consumer cursor survives pod death AND redeploys — chaos-verified live: a trigger published while the
> mover was scaled to 0 sat as `Unprocessed: 1` on the durable and was delivered on recovery, and a
> rollout restart re-attached with zero `consumer name already in use` errors (that orphan mode applies
> to durables *without* a queue group). See gap #3 for what remains.

## Where it CAN bite — the real gaps (honest)

1. **The catalog outbox gap (the #1 weakness).** The catalog emits lineage **inline-awaited + best-effort**
   *after* the Lance write commits to S3 (`services/catalog/core/lineage_emit.py`) — awaited (not a
   BackgroundTasks fire-and-forget) so the event reaches the durable Dapr/JetStream transport before the
   response returns, but its errors are swallowed so a lineage outage never fails the write. If the catalog
   crashes (or its sidecar is down) **between the S3 write and the publish**, the data exists on storage but
   **the lineage event is lost** — the graph under-reports that write. No corruption, but a provenance hole.
   *Mitigation (shipped):* the **B4 storage→graph reconcile** back-fills exactly this loss mode — a Dapr-cron
   sweep reads on-disk Lance versions and stamps any write the graph is missing (`/datasets/{name}/reconcile`
   + `services.lineage.reconcile`). The **transactional outbox** (`common.outbox`, opt-in via `LINEAGE_OUTBOX_URI`)
   closes the window fully for producers that stage the event before publishing. *Ops surface (#83):* the outbox
   backlog is now viewable + replayable on demand — `GET /admin/dlq` (the saturation snapshot + the governed
   at-risk events) and `POST /admin/dlq/{run_id}/replay` (re-ingest one event, then drop; same `can_write_data`
   authz as a fresh ingest, audited), surfaced in the frontend at `/dlq`. This is the manual twin of the
   reconcile relay's drain — an operator no longer waits for the next tick. *Full fix:* make the **Ray job the
   durable producer** (it owns the write + the emit) — the documented direction ([`FLOW.md`](FLOW.md) §7, [`RASK-INTEGRATION.md`](RASK-INTEGRATION.md)).

2. **No dead-letter queue; `maxDeliver=5` — FIXED 2026-07-12 (`dapr.resiliency.enabled`, DEFAULT ON;
   `false` = the exact pre-existing broker-only behavior).** The gap as it stood: a genuinely poison
   message (always `RETRY`, not malformed) was dropped from the *consumer* after 5 deliveries
   (~8.5 min of backOff) with **no DLQ**; limits retention kept it in the *stream*, so the lineage
   consumer (`deliverPolicy: all`) re-saw it on restart — but an outage longer than the retry window
   meant the event wasn't ingested **until a restart**. The fix is the Dapr-native SET (only correct
   together — a `deadLetterTopic` without a retry policy dead-letters on the FIRST failure, Dapr's
   documented default): a **Resiliency CRD** makes the sidecar own delivery retries (exponential
   30s→300s, 5 attempts ≈ the old broker schedule), every subscription declares a per-app
   **`deadLetterTopic`** (`dlq.*`, parked on the dedicated **DLQ stream** — load-bearing: Dapr does
   not auto-create streams, so without it the parking publish itself would fail), exhausted
   deliveries PARK there — ERROR-logged (`dapr_dead_letter_parked`) by each app's `/dlq-event`
   route, acked, never blind-requeued — and the broker `backOff` moves to a 720s crash-recovery
   window so it can't race the sidecar's retries. The durable-consumer question was ANSWERED FROM
   SOURCE (components-contrib `jetstream.go` applies `durableName` as-is per subscription; NATS
   scopes consumer names PER STREAM; the dlq topics live on their own stream — and the producer
   already runs two same-durable subscriptions across two streams live). CI render-asserts the set
   ships together and the escape hatch restores the chaos-verified schedule. Live check remaining:
   the runbook 6.5 poison-inject. The once-planned **durable PULL consumer** is RETIRED (2026-07-12): PULL means consuming NATS directly (nats-py), i.e. leaving Dapr pub/sub — which contradicts the pinned Dapr-first rule — and its target gaps (cursor loss, silent exhaustion) are since covered by durable push cursors + sidecar Resiliency retries + this DLQ. Revisit only if a live delivery-semantics gap appears that Dapr's model cannot express.

3. **Trigger loss on mover death: FIXED (durable cursors, 2026-07-06); lineage full-stream-replay
   remains by design.** The cascade head + movers now pair `deliverPolicy: new` with a `durableName`
   (`chart/templates/dapr-component.yaml`): the consumer cursor survives pod death and redeploys, so a
   trigger published while a mover is down is **delivered on recovery** instead of skipped —
   chaos-verified live (publish-while-scaled-to-0 → `Unprocessed: 1` retained → processed on scale-up;
   post-redeploy consumption clean). Lineage deliberately stays ephemeral + `deliverPolicy: all`: each
   restart replays the retained stream into the idempotent MERGE (O(stream size) load, zero loss) —
   a durable cursor there would defeat the replay-rebuilds-the-graph recovery story. Residual: the
   replay load on lineage restarts, and gap #2's poison/no-DLQ window, which durable cursors do not
   change.

4. **Best-effort durable feed.** The `/events` feed table write is best-effort (logged on failure); the
   AGE graph is authoritative. The feed can lag the graph — visibility, not correctness.

5. **Single points of failure (single-node infra).** Every stateful backing store runs as **one replica** on
   kind — this is a **demo topology, not an HA one**. If any of these pods dies, the pipeline stalls until it
   restarts (no data loss — PVCs persist — but no availability either):
   - **AGE / Postgres** — one StatefulSet pod; the whole lineage graph + the durable `/events` + `/runs` fold
     live here. Down → all ingest returns `RETRY` (buffered) and every lineage read fails.
   - **NATS / JetStream** — one node, `streamReplicas=1`; the Dapr pub/sub backbone. Down → no event flows at
     all (catalog emits swallow, the cascade halts).
   - **RustFS** — one pod (now on a keep-PVC, not emptyDir); the S3 data plane. Down → no Lance read/write.
   - **OpenBao / OpenFGA / Dex** — one pod each; down → (respectively) app-tier secrets fail-closed at boot,
     authz checks fail, OIDC login fails.
   *Fix (prod):* multi-replica + `streamReplicas=3` for NATS, a real Postgres HA (Patroni/CNPG), a replicated
   object store. Tracked as infra work; **the app tier is already `replicas`-ready** (stateless, PDBs shipped).

6. **Lineage lifecycle-emit gaps (deferred, see `docs/DECISIONS.md`).** Overwrite leaves stale column nodes on
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
   the old config blocked all 4 movers + lance-ray after the image roll. It **self-heals**:
   JetStream reaps the old unbound durables at their `inactive_threshold` (~20–25 min observed —
   rollout 06:53–07:00 UTC → reaped + recreated 07:19–07:20), the sidecars recreate them with the
   new config, and delivery resumes with **no manual intervention**. *Fast cutover (operators):*
   right after the rollout, `nats consumer rm` the `<app>-durable` consumers on
   LINEAGE/MEDALLION/TRAINING/DLQ — the sidecars recreate them within seconds. Fresh installs and
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
  (compaction gets a longer budget for an in-flight sweep) so endpoint removal propagates before SIGTERM —
  in-flight requests drain instead of connection-refusing (`lance.preStop`, `lifecycle.*`).
- **Bounded Dapr publishes.** Every `publish_event` is wrapped in `asyncio.timeout`
  (`common/dapr_publish.py`) so a wedged sidecar/NATS can't hang a write handler past its ack window.
- **Liveness + readiness probes on every workload.** App pods get an HTTP `readinessProbe` (`/readyz`,
  dependency-aware) **and** `livenessProbe` (`/livez`, process-up only — never restarts on a slow backend);
  web + RustFS get `tcpSocket` probes (`lance.appProbes` / `lance.tcpProbes`).
- **Restricted container securityContext on every app pod.** `runAsNonRoot` (images ship a numeric
  `USER` — uid 10001 catalog, 1000 web — so the kubelet can verify non-root at admission), drop **ALL**
  capabilities, no privilege escalation, `seccompProfile: RuntimeDefault`, and `readOnlyRootFilesystem`
  with a writable `/tmp` emptyDir for scratch (`lance.securityContext`, `security.readOnlyRootFilesystem`).
- **Single-flight writes + reconcile.** The cascade stage write is serialized by a process-wide lock so a
  redelivered trigger can't race a second `mode="overwrite"` onto the same target
  (`transform.py:_write_lock`, moverReplicas=1). The B4 reconcile sweep runs under a Postgres
  advisory lock (`repository.reconcile_lock`) so the per-replica cron can't double-drive a back-fill. And a
  **UNIQUE index on every AGE vertex label's MERGE key** (`ensure_graph_constraints` at boot + the age-init
  SQL) makes AGE's MATCH-then-CREATE safe under concurrency — a racing duplicate `CREATE` is rejected by the
  DB (`duplicate key value violates unique constraint "lineage_run_uniq"`) instead of leaving a dup vertex.
  Ingest MERGEs datasets in a **deterministic name-sorted order** so two concurrent ingests that first-create
  the same vertices acquire the index locks in the same order — no deadlock (the index's only new failure mode).

## Prod hardening backlog (delegate-to-platform — NOT dev-blocking)

The 2026-07-05 don't-reinvent audit confirmed we **reinvent nothing k8s/Dapr owns** (zero code to delete), but
several **native switches are off** — deliberately deferred because they are prod-only (kind's default CNI
ignores NetworkPolicy) and footgun-sequenced. Full list + fix order in [`docs/DECISIONS.md`](DECISIONS.md#12--prod-hardening-backlog-native-switches-off) §12:
the big one is **L3 network** (no default-deny; the OpenBao secret store is reachable by any pod today),
then **least-privilege ServiceAccounts** (~13 pods on `default` with a mountable API token), **infra-pod
securityContext** (app tier hardened, infra not), and **Pod Security Admission** enforcement. Do not rush
these into the dev baseline — default-deny egress without a kube-dns allow bricks the cluster, and restricted
PSA would reject `lineage`/`openfga-migrate` until their root init containers are hardened.

## Bottom line

- **Corruption:** not possible on the event path — idempotent MERGE on `run_id`.
- **Loss:** only at the catalog outbox gap (crash between S3 write + publish). Everything downstream of a
  successful publish is durable + replayed.
- **Recovery:** automatic — `RETRY` for transient faults, JetStream buffer + replay for downed services.
- **Hardening roadmap (for prod):** ~~durable PULL consumer~~ (RETIRED — contradicts the Dapr-first
  rule; superseded by durable push cursors + Resiliency retries) · ~~Dapr `deadLetterTopic`~~
  (SHIPPED, default on) · transactional outbox / Ray durable producer (the remaining item —
  belongs to the rask merge, where the Ray job becomes the durable producer).
