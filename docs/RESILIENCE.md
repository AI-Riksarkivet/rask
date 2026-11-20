# Resilience & failure modes

What happens when a service goes down, can we get corrupted state, and how do we recover.

> **The standing chaos suite.** Criterion 5's drills (`tests/e2e-py/test_the_estate_is_resilient.py`, [[XC-095]]) take
> one dependency down at a time (the object store, OpenFGA, lineage, the bus, the issuer, the secret store) or kill the
> catalog mid-commit, drive the doors a pipeline uses while it is gone, restore it, and hold the estate to criteria 1
> and 4: every version a commit the pipeline made, one edge for each input it names, no delivery parked by lineage's
> ingest lane, both outboxes drained. The e2e-stack kind lane runs them on every push, last, with `--chaos`; the deployed release runs
> them only when invoked explicitly (`bash scripts/e2e_live.sh -m phase1 --require-live --chaos`), and a plain live
> drive selects none. Their measured record is the next section; the table after it is the earlier point-in-time
> record, each row run once by hand.

## The core guarantee: at-least-once + idempotent → no corruption, no loss (within bounds)

The event-driven path is `catalog --publish on its own connection--> JetStream (LINEAGE / CATALOG_CONTROL streams) --> consumer --pulled or followed on the service's own connection--> service`.
Two properties make it safe:

1. **Durable buffer.** Both streams use **Limits retention** (messages persist after ack, up to `max-age`
   168h), so an event survives the subscriber being down.
2. **Idempotent ingest.** Lineage records each event in the commit feed (`lance_catalog$provenance$events`) and the edge list
   (`lance_catalog$provenance$edges`) with an insert-only merge on each table's unenforced primary key, through the catalog's
   `merge_insert` door (`lineage/services/provenance.py`). So redelivery / replay of the same event lands no
   second row — never a duplicate, never corruption. The worst failure mode is *under-reporting* (a lost event), never a corrupt or doubled one.

## Criterion 5's drills, as measured

Each drill ends with the readings criteria 1 and 4 hold the scenario to. Every version is a commit the pipeline made,
carrying the run it named. The edge list holds one edge per input those versions name, and at most one for a commit whose
answer was lost (LH-497). Lineage's ingest lane parks no delivery, and both of the catalog's outboxes drain. The durations are each drill's
whole run, outage and recovery included, on the e2e-stack kind lane (run 39049821010).

| Dependency | Taken down | What the doors answer while it is gone | Kind lane, 2026-10-17 |
| --- | --- | --- | --- |
| Object store (`rask-rustfs`)¹ | scaled to zero | A write through the catalog's insert door is refused 503 code 17, or gets no answer within the bound the catalog's own store timeouts set (2 × 6 listings × (retries + 1) × max(connect, request): 60 s at the defaults). It never lands while the store is gone. | 24 s |
| OpenFGA | scaled to zero | A governed write answers 503 code 17 at its authorization check: never an allow, and never a deny that would read as a missing grant. | 20 s |
| Lineage | scaled to zero | Commits go on landing; its durable consumer resumes where it stopped and records each announcement once. | 39 s |
| The bus (`rask-nats`, three nodes) | scaled 3 → 0 for 150 s | Commits and grants land, their announcements staged in the outboxes, and the lineage relay records the staged writes without the bus. Each service's own connection is opened again by its own loop (`lakehouse.events.bus`), and its consumers are pulled or followed again on it (below). | 320 s, 150 s of it the outage² |
| Dex | its Service cut from its pod | A door holding the issuer's key set (300 s) accepts an existing token. A door that must fetch the key set answers 503 code 17, never 401, and accepts the token again once Dex is back, with no restart. | 69 s |
| The secret store (`rask-openbao`) | sealed in place | Within one re-read of its key (300 s) the catalog drops its signing key, then refuses every write it would announce with 503 code 17 before committing it. The store's contents survive the seal, so nothing is minted again. | 290 s, most of it the re-read |
| The catalog | its uvicorn SIGKILLed, four writers committing through `version/create` | Every acknowledged commit lands once, a commit whose answer the kill lost lands at most once, and no run lands twice: Lance's put-if-not-exists of each `_versions/` manifest keeps one history. | 168 s |

¹ Measured on the RustFS fixture (ADR 0168): run 39129931426, main's e2e-stack lane, 2026-10-18; the drill passed
again on the estate rebuilt on RustFS from empty the same day (helm rev 1, "XC-163 from empty 9858db38").

² Measured 2026-10-17 on the sidecar delivery path. The drill has not been run on the pull consumers (XC-199 step D4,
2026-11-14).

**A bus outage of any length leaves every service pulling again once the bus is back, with no pod restarted.** Each
service publishes and takes its deliveries on a connection of its own (`lakehouse.events.bus`). While that connection is away
a publish fails at once, so its event stays staged in the outbox for its relay. nats-py reconnects by itself through a
short cut; past its attempts, or when the bus ends the session, the service's own loop (`Bus.keep_connected`) opens a
new client with the credential the secret store holds then. A pull consumer (`Bus.consume`) is pulled again on the new
connection, where the bus has kept every message the service had not settled; the catalog's control topic
(`Bus.follow`) is followed again from the message after the last one it handed on. The connection loop is measured
against a three-node bus on the nats-auth lane (`tests/e2e-py/test_nats_admits_each_client_only_to_its_own_subjects.py`).

The maintenance worker holds its work queue on a connection of its own (`maintenance.services.queue.TaskQueue`), which
reconnects for ever. A bus that refuses its credential or ends its session ends the replica, and Kubernetes starts it
again with the credential the store holds then.

## Chaos experiments (all recovered with zero loss)

| Experiment | What happened | Result |
| ---------- | ------------- | ------ |
| **Pull `lineage` (scale 0), publish 3 events, restart** | events buffered in JetStream (70→73); on restart the **ephemeral consumer replayed** them | **3/3 ingested** — no loss |
| **Poison-inject 2026-09-14: a NON-CloudEvent body** (`rawPayload`) | the sidecar could not deserialize the envelope, dead-lettered it in **21ms with ZERO retries** (ahead of any retry policy), and the dead letter reached the DLQ topic as the same unreadable bytes | **parked INVISIBLY** — `/dlq-event` never ran, so no ERROR log and nothing for a dead-lettering alert to fire on. Tracked as LH-151 |

Since 2026-11-14 (XC-199 step D4) a body that is no event envelope reaches lineage's own handling, which logs
`lineage_event_invalid` at ERROR, counts it `unrepairable` and acknowledges it; nothing parks it unseen.

So: **a service going down delays the pipeline; it does not lose or corrupt data.** A transient dependency failure is
asked for again by the lane that met it, on the numbers that lane names (`lakehouse.events.bus_delivery.judged`):

- **Lineage** handles a delivery up to `services.lineage.delivery.attempts` (5) times, `retrySeconds` (120) apart, so it
  rides out an outage of 8 minutes. A handling that raises, or runs past the consumer's ack wait (`ackWaitSeconds`,
  300) less the 10 s kept for its settle, counts as asked for again. On its last attempt the delivery is parked (gap 2).
- **The maintenance worker** hands an announcement it cannot settle back with a delay drawn at random up to a bound
  that doubles with each delivery, and ends it on its `maintenance.queue.maxDeliver`-th (10): the table's recheck
  backs it up.
- **The catalog's control lane** judges an event whose signature keys it cannot read 5 times, 15 s apart, then gives it
  up (`bus.deliveries` outcome `failed`): the event is a refresh hint, and the audit trail is the record. It waits that
  way at most once in five minutes: a replica is handed one event at a time, so a wait holds back every event behind
  it, and until the five minutes have passed an event whose keys cannot be read is given up on its one judgement.

> **Delivery semantics per consumer.** The stream Job declares every durable consumer (`lance.busConsumers` in
> `chart/templates/_helpers.tpl`), and each service binds to its own and creates none. Lineage's replicas share
> `LINEAGE/lineage-ingest`, so each event is delivered to one of them, and the consumer keeps its place across pod death
> and redeploys. It is delivered by start sequence: a consumer created afresh begins at the stream's first message, so
> replay into the insert-only merge is the durability story, and the first chaos row holds. The worker's replicas share
> `LINEAGE/maintenance-arrival`, delivered from the events published after its creation (`new`), so an event published
> while every worker was down is delivered on recovery. Each catalog replica follows `CATALOG_CONTROL` by
> reading it by sequence, so every replica sees every event, in the stream's order.

## Where it CAN bite — the real gaps (honest)

1. **The catalog outbox gap (the #1 weakness).** The catalog emits lineage **inline-awaited + best-effort**
   *after* the Lance write commits to S3 (`services/catalog/src/catalog/core/lineage_emit.py`) — awaited (not a
   BackgroundTasks fire-and-forget) so the event reaches JetStream, over the catalog's own connection, before the
   response returns, but its errors are swallowed so a lineage outage never fails the write. If the catalog
   crashes (or the bus cannot be reached) **between the S3 write and the publish**, the data exists on storage but
   **the lineage event is lost** — the feed under-reports that write. No corruption, but a provenance hole.
   *Mitigation (shipped):* the **object-store outbox** (`lakehouse.events.outbox`, opt-in via the
   catalog's `LANCE_LINEAGE_OUTBOX_URI` and the relay's `LINEAGE_OUTBOX_URI`) narrows the window to commit→stage
   for producers that stage the event before publishing. A crash between the write and the stage is not detected:
   nothing compares on-disk versions with the feed (parked as LH-497, "the commit is the outbox"). *Ops surface (#83):* the outbox
   backlog is now viewable + replayable on demand — `GET /admin/dlq` (the saturation snapshot + the governed
   at-risk events) and `POST /admin/dlq/{run_id}/replay` (re-ingest one event, re-publish it, then drop; same `can_write_data`
   authz as a fresh ingest, audited), surfaced in the frontend at `/dlq`. This is the manual twin of the
   relay tick's drain, for an operator who does not want to wait for the next tick.

2. **A delivery lineage cannot record is parked where the estate admin can replay it.** A pull consumer has no dead
   letter, so the rule that ends a delivery is the lakehouse's own (`lakehouse.events.bus_delivery.judged`, pinned by
   `packages/lakehouse/tests/test_a_delivery_is_settled_by_what_its_handling_answers.py`). Lineage parks a delivery on
   its last attempt (above), and at once when its handling refuses a named person who lacks a grant, since a grant can
   repair it. A park keeps the delivery in the provenance namespace's `refusals` table under `dead_letter:<CloudEvent
   id>` (or, for an envelope that carries no id, under the key of the feed row it would write), unless the feed already
   holds its row; logs `lineage_delivery_parked` (ERROR, or WARNING when the row is
   already recorded); counts `lineage.events.processed` at door `dead_letter`, outcome `dead_lettered` or
   `parked_already_recorded`; and then ends the message on the bus. A park the store did not take is asked for again,
   so a park is never lost. Nothing is published to a dead-letter topic. The estate admin lists the parks at
   `GET /admin/dlq/parked` and records one with `POST /admin/dlq/parked/{run_id}/replay` (see
   [RUNBOOK-oncall § DLQ parking](runbooks/RUNBOOK-oncall.md#dlq-parking--a-delivery-gave-up)). A changed ack wait or
   in-flight bound reaches the consumer when the stream Job edits it in place; a changed attempt count or delay reaches
   lineage with its pods' next start.

3. **Durable cursors; lineage full-stream-replay by design.** Lineage's and the worker's consumers are durable
   ([[LH-303]]): the consumer keeps its place across pod death and redeploys, so an event published while a service is
   down is **delivered on recovery** instead of skipped. `lineage-ingest` is delivered by start sequence, and the
   stream Job declares the start it holds back to it on every run, since a consumer's start cannot be edited. Created
   afresh it begins at the stream's first message and replays the retained stream into the idempotent ingest. To
   REBUILD the feed from the stream, remove the consumer and run the declare phase again: `nats consumer rm LINEAGE
   lineage-ingest` as `admin` ([OPERATORS.md](OPERATORS.md) § 7: nats-box itself runs as the read-only `monitor`),
   then a reconcile (an Argo CD sync, or `helm upgrade`), which creates it at the first message; lineage's replicas pull
   it as soon as it exists. The catalog's control topic needs no durable consumer: each replica reads the stream by
   sequence, begins after the stream's last message when it first connects, and asks for the message after the last it
   handed on, through a read the bus refused and a connection it lost ([[LH-305]]). A stream removed and made again
   numbers its messages from 1: a replica that finds the topic's last message behind its place begins at the new
   stream's first and logs `bus_follow_began_again`.

4. **The feed is the record.** Lineage writes the edges, then the feed row, each keyed; a failed write asks for the
   delivery again, and the redelivery lands what is missing and nothing twice. The feed keeps `eventsRetentionDays`
   (default 7) and is pruned on the relay tick; the long-lived graph is the forwarded backend's
   (`services.lineage.forward.url`), fed at least once, in feed order, from a cursor in `lance_catalog$provenance$cursors`.

5. **Single points of failure (single-node infra).** Every stateful backing store runs as **one replica** on
   kind — this is a **demo topology, not an HA one**. If any of these pods dies, the pipeline stalls until it
   restarts (no data loss — PVCs persist — but no availability either):
   - **AGE / Postgres** — one StatefulSet pod serving OpenFGA's datastore. Down →
     authz checks fail (lineage's ingest gate asks for each delivery again, so events stay buffered for its 5
     attempts and are parked after them, gap 2). Lineage keeps no database: its tables live in the object store behind the catalog.
   - **The catalog** — lineage's feed and edge list are read and written through its doors. Down → lineage
     ingest asks for each delivery again (buffered, then parked, as above) and lineage's `/readyz` reports the catalog unavailable.
   - **NATS / JetStream** — three nodes, `streamReplicas=3`, each on its own volume; the one bus every event rides. All
     down → no event flows: the catalog's writes still land and stage their announcements in its outboxes, which the
     relays deliver once the bus is back.
   - **The object store** (the RustFS fixture in development) — the S3 data plane. Down → no Lance read/write.
   - **The secret store / OpenFGA / Dex** — one pod each in development (the store and Dex are fixtures; production
     names the platform's). The store sealed: a signer drops its key within one re-read (300 s) and the catalog refuses
     every write it would announce, and the bus is dialled with the pod's own token, not a credential read from the store. OpenFGA down: every governed door answers 503 code 17. Dex down: a door that still holds
     its key set accepts the tokens it verifies for up to 300 s, and one that must fetch the key set answers 503 code 17.
   *Fix (prod):* multi-replica + `streamReplicas=3` for NATS, a real Postgres HA (Patroni/CNPG), a replicated
   object store. Tracked as infra work; **the app tier is already `replicas`-ready** (stateless, PDBs shipped).

6. **Lineage lifecycle emits.** **deregister** emits a lineage marker. **rename** emits one `RENAME` dataset change on
   the new name, naming the old one as `previousIdentifier` and the table's uuid, so lineage follows the table across it
   (ADR 0173 §10, ADR 0191).

7. **A consumer's config converges on every reconcile, and a change the bus refuses fails the reconcile.** The services
   bind to the consumers the chart declares and create none, so no consumer exists under a config the chart does not
   describe. The stream Job's declare phase edits every declared consumer to its rendered config in place, as it does
   every stream. Measured on natscli 0.5.1 against nats-server 2.15.2, 2026-11-14: a changed `ack_wait`,
   `max_ack_pending`, filter or metadata is applied in place, an unchanged config edits to no change, and a changed
   start sequence or `max_waiting` is refused (`... can not be updated (10012)`). A refused edit fails the Job with the
   server's reason, and the services keep pulling the consumer as it stands. The retire phase, after the sync, removes
   each consumer the chart no longer declares that carries the release's mark (`rask.dev/release`); a consumer with no
   mark is another subscriber's and is never touched. Each phase's log is its Job's
   (`kubectl logs job/<release>-nats-stream-r<rev>`, `kubectl logs job/<release>-nats-stream-retire-r<rev>`).

## Operational hardening (k8s posture)

Cloud-native guards on the *deployment*, complementing the event-path guarantees above. All applied via the
chart and live-verified on kind.

- **No plaintext secrets in the render.** OpenFGA's datastore DSN comes from `secretKeyRef`
  (`datastore.existingSecret`), not a hardcoded `postgres://lance:lance@…`; the S3 secrets are
  consumed from OpenBao at boot when a secret store is configured. `helm template -f values-prod.yaml` shows
  zero plaintext secret values in env/args.
- **Graceful rollout drain.** Every app Deployment has a `preStop` sleep + `terminationGracePeriodSeconds`
  (maintenance gets a longer budget, so a running table pass finishes inside `maintenance.queue.drainSeconds`) so endpoint removal propagates before SIGTERM —
  in-flight requests drain instead of connection-refusing (`lance.preStop`, `lifecycle.*`).
- **Bounded publishes.** Every `lakehouse.events.bus.publish_event` runs under its caller's `timeout_seconds`, and a publish
  with no connection fails at once, so a bus that does not answer cannot hold a write handler.
- **Liveness + readiness probes on every workload.** App pods get an HTTP `readinessProbe` (`/readyz`,
  dependency-aware) **and** `livenessProbe` (`/livez`, process-up only — never restarts on a slow backend);
  a workload with no HTTP health route gets a `tcpSocket` probe (`lance.appProbes` / `lance.tcpProbes`).
  `/readyz` answers from memory: each dependency is read in the background every 5 s (`lakehouse.process.health`);
  the catalog's store and OpenFGA and lineage's feed gate, and the bus, the secret store, lineage's OpenFGA and
  maintenance's and lineage's object store are reported only (ADR 0203).
- **A background loop that ends ends its process.** Every loop a lakehouse process starts runs under
  `lakehouse.process.supervision.Supervisor`; one that ends while the process serves ends it (exit 70) so Kubernetes
  starts the container again, rather than leaving a Ready pod that does nothing (ADR 0203).
- **Every request has a deadline and a panic catcher.** A request with no answer begun after
  `requestDeadlineSeconds` (300 s) is answered 503 code 17 and its door runs on to its commit or its undo; a native
  panic inside one is a 500 problem body, and the process serves on (ADR 0203).
- **Restricted container securityContext on every app pod.** `runAsNonRoot` (images ship a numeric
  `USER` — uid 10001 — so the kubelet can verify non-root at admission), drop **ALL**
  capabilities, no privilege escalation, `seccompProfile: RuntimeDefault`, and `readOnlyRootFilesystem`
  with a writable `/tmp` emptyDir for scratch (`lance.securityContext`, `security.readOnlyRootFilesystem`).
- **A timer's tick runs once a window.** Every replica of the catalog and of lineage runs each of its fixed timers'
  loops, and one conditional write of the timer's claim record under the control root (`_timers/<timer>.json`,
  `lakehouse.timers`) picks the replica that runs the window's tick, as Lance picks one writer of a version. A window is
  named by when it began on the store's clock, so no two pods' clocks are compared. A timer whose claims stop moving
  pages (`LakehouseTimerStopped`).
- **A relay tick that overlaps the next needs no lock.** Every step of lineage's relay is idempotent
  (`lineage/api/relay.py`): a drained event is recorded under its feed key, so a second record
  lands no row; two prunes delete the same rows once; the forwarder's cursor moves only forward, so a slower
  replica's tick never undoes a faster one's.

## Invariants of the lakehouse's own background work

The lakehouse runs no batch pipeline ([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)); a
pipeline's own invariants are its engine's business. One rule binds the work the lakehouse does run, and a test fails
without it:

- **A stopping process parks nothing it was handling.** A delivery whose handling is cut because its process stops is
  settled by nobody (`lakehouse.events.bus_delivery.judged` lets the cancellation through, and parks nothing): `Bus.consume`
  hands it back to the bus at once, so another replica takes it without waiting out the ack wait, and a good message
  is never parked for a human to find. Pinned by
  `packages/lakehouse/tests/test_a_delivery_is_settled_by_what_its_handling_answers.py`.

## Prod hardening backlog (delegate-to-platform — NOT dev-blocking)

The 2026-07-05 don't-reinvent audit confirmed we **reinvent nothing Kubernetes owns** (zero code to delete), but
several **native switches are off** — deliberately deferred because they are prod-only (kind's default CNI
ignores NetworkPolicy) and footgun-sequenced. Full list + fix order in [`docs/adr/0009-12-prod-hardening-backlog-native-switches-off.md`](adr/0009-12-prod-hardening-backlog-native-switches-off.md) §12:
the big one is **L3 network** (no default-deny; the OpenBao secret store is reachable by any pod today),
then **least-privilege ServiceAccounts** (~13 pods on `default` with a mountable API token), **infra-pod
securityContext** (app tier hardened, infra not), and **Pod Security Admission** enforcement. Do not rush
these into the dev baseline — default-deny egress without a kube-dns allow bricks the cluster, and restricted
PSA would reject `lineage`/`openfga-migrate` until their root init containers are hardened.

## Bottom line

- **Corruption:** not possible on the event path — insert-only merges on each provenance table's primary key.
- **Loss:** only in the commit->stage window the outbox narrows the gap to (a crash between the write and
  the stage, or a stage that fails AND a publish that fails). Everything staged or successfully published is
  durable + replayed.
- **Recovery:** automatic — a delivery asked for again for transient faults, JetStream buffer + replay for downed
  services, and a parked delivery replayed by the estate admin.
- **Hardening roadmap (for prod):** durable pull consumers the chart declares + each lane's retry and park (shipped)
  · a transactional outbox that closes the commit→stage window (the remaining item).
