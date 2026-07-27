# On-call runbook

Symptom → cause → action for the known failure modes of the Lance-lakehouse estate. Grounded in how this
stack actually fails. With alerting enabled (`observability.alerting.enabled`, on in prod), the proven
rules (`chart/alerting/rules.yml`) page via vmalert → Alertmanager — note the live vmalert→GreptimeDB
round-trip is still an unrehearsed drill (see [DECISIONS.md "P3b"](DECISIONS.md)). On kind (alerting off),
these are found by watching the Perses dashboards (`make dashboards`) or a user report — not a page.

## Orientation

- **Services** (FastAPI): `catalog` (REST catalog + model registry), `lineage` (OpenLineage → Apache AGE
  graph), the medallion **movers** (`raw-to-bronze`, `bronze-to-silver`, `silver-to-gold`, `media-to-silver`),
  `lance-ray` (the `/produce` + `/train` head), `compaction`, `gateway`, `web`.
- **Stores**: `age-postgres` (the lineage graph **AND** OpenFGA's datastore — one pod, dual-purpose),
  `rustfs` (the Lance object store — all medallion + registry data), `openfga` (authz), `openbao` (secrets,
  prod = sealed file backend), `greptimedb` (metrics), `dex` (OIDC).
- **Signals**: Perses panels (`make dashboards`), `kubectl logs`, and the domain metrics —
  `lineage_events_processed_total{lance_lineage_outcome}` (incl. `DEAD_LETTERED`),
  `medallion_stage_transitions_total{lance_medallion_transition}`, `medallion_dlq_parked{lance_medallion_app}`,
  `outbox_depth` / `outbox_oldest_age`, `lance_training_*`.
- **First move for any pod issue**: `kubectl get pods -o wide`, then `kubectl logs <pod> --all-containers
  --tail=100` and `kubectl describe pod <pod>` (Events section).

## Symptom index

| Symptom | Jump to |
|---|---|
| Everything returns 503 / auth failures across the board | [OpenFGA down](#openfga-down--503-everywhere) |
| App pods stuck `0/1` "waiting for application startup" after a restart/drain | [OpenBao sealed](#openbao-sealed-on-restart--boot-deadlock) |
| A pod `CrashLoopBackOff` shortly after boot | [Slow boot / startupProbe](#crashloop-on-boot) |
| `/readyz` = 503 `{"database":"unavailable"}`, pod `NotReady` | [readyz degraded](#readyz-degraded--pool-or-graph) |
| Cascade stops mid-way (bronze but no silver, etc.) | [Cascade stalled](#cascade-stalled) |
| `medallion_dlq_parked` rising / `dapr_dead_letter_parked` ERROR logs | [DLQ parking](#dlq-parking--a-delivery-gave-up) |
| `outbox_depth` sustained > 0, `outbox_oldest_age` climbing | [Outbox not draining](#outbox-not-draining) |
| Catalog reads/writes fail, Lance data unreachable | [RustFS down](#rustfs-down--data-plane) |
| No lineage, `/runs` empty or erroring, FGA also failing | [AGE down](#age-postgres-down--lineage--fga) |

---

## OpenFGA down → 503 everywhere

**Symptom.** Broad 503s: catalog writes, lineage reads, AND the cascade all fail at once. Logs show
`authorization service is not available` or FGA check timeouts.

**Cause.** OpenFGA is the authz chokepoint — every governed call `fga.check`s it and **fails closed**. A
single-replica OpenFGA (kind) that is drained/OOMed/rescheduling takes the whole estate down until it
returns. The prod overlay runs 3 replicas + a PDB, which makes this a quorum event, not a total outage.

**Diagnose.** `kubectl get pods -l app.kubernetes.io/name=openfga`; check its datastore (`age-postgres`) is
up — OpenFGA stores tuples in the same Postgres as the lineage graph.

**Act.** Wait for reschedule / scale it back up; if the datastore is the real cause, see
[AGE down](#age-postgres-down--lineage--fga). Never "fix" a fail-closed 503 by disabling auth in prod.

## OpenBao sealed-on-restart → boot deadlock

**Symptom.** After a node drain / pod restart, app pods hang `0/1` at "waiting for application startup"; the
startupProbe eventually restarts them, looping.

**Cause.** Prod OpenBao (`devMode=false`) is a **sealed** file backend. Apps consume secrets fail-closed via
Dapr **at startup** (the ~80s secret fetch), so while OpenBao is sealed every app pod blocks on boot — the
documented two-sided deadlock. There is no auto-unseal yet (ESO / bank-vaults is the destination —
ASSESSMENT gap #5).

**Diagnose.** `kubectl exec <openbao-pod> -- bao status` → `Sealed: true`.

**Act.** `bao operator unseal` with the unseal key(s), then the app pods complete boot on their next
startupProbe cycle (no manual app restart needed — the 300s budget covers it). **Prevention:** adopt an
auto-unseal operator before calling the secret tier prod-ready.

## CrashLoop on boot

**Symptom.** An app pod restarts repeatedly shortly after scheduling; logs show the lifespan never finishing.

**Cause.** The FastAPI lifespan (Dapr secret fetch ~80s worst case + AGE pool + DDL + FGA provision) runs
**before** uvicorn serves. The `startupProbe` gives 300s for this; if boot exceeds it, a dependency is
genuinely stuck — usually OpenBao sealed (above) or AGE unreachable.

**Diagnose.** `kubectl logs <pod> --previous` for where the lifespan stalls; check OpenBao + AGE.

**Act.** Fix the upstream dependency; the pod recovers on its own once boot can complete.

## /readyz degraded → pool or graph

**Symptom.** `/readyz` returns 503 `{"status":"degraded","database":"unavailable"}`; the pod goes `NotReady`
and is pulled from rotation (correct — it stops serving 500s).

**Cause.** `/readyz` gates on BOTH the AGE pool (`SELECT 1`) and the **graph** (`RETURN 1`). A graph
failure (an external managed-PG that was never bootstrapped, a failed `create_graph`, a bad restore) now
fails readiness LOUDLY instead of silently discarding events.

**Diagnose.** `kubectl exec <age-pod> -- psql -c "LOAD 'age'; SELECT count(*) FROM ag_catalog.ag_graph;"` —
is the `lineage` graph present? Is the pool reachable?

**Act.** If the graph is absent on managed PG, lineage self-heals via `ensure_graph()` on next boot —
restart the pod. If the pool is down, see [AGE down](#age-postgres-down--lineage--fga).

## Cascade stalled

**Symptom.** `/produce` ran but the cascade stops mid-way — e.g. `bronze$events` exists but `silver$features`
never lands. `medallion_stage_transitions_total` flat for the missing stage.

**Cause (in order of likelihood).** (1) The stage mover was **FGA-denied** — it lacks the grant to write its
target (`medallion_stage_denied` rises); re-seed with `scripts/seed_medallion_fga.sh`. (2) A **quality gate**
blocked a bad batch (`medallion_stage_quality_blocked`) — by design, the bad batch does not promote; the
failed run is in the lineage graph. (3) With Ray on, the stage Ray job failed/timed out — check
`ray job list` on `ray-lance-head`. (4) The delivery exhausted retries and parked — see
[DLQ parking](#dlq-parking--a-delivery-gave-up).

**Diagnose.** Mover logs (`kubectl logs -l app.kubernetes.io/component=bronze-to-silver`); grep
`medallion_stage_denied` / `medallion_quality_blocked` / `ray_stage_job`.

**Act.** Re-seed FGA for a denial; a quality block is expected (fix the source data); resubmit/replay after
fixing a Ray-job cause.

## DLQ parking → a delivery gave up

**Symptom.** `medallion_dlq_parked{lance_medallion_app}` rises and/or `dapr_dead_letter_parked` ERROR
logs. A specific cascade item is permanently stalled after exhausting its retry schedule.

**Cause.** Dapr's Resiliency retry policy was exhausted for a delivery, so the sidecar published it to the
DLQ topic and acked the original (no infinite retry loop). The message is retained in JetStream.

**Diagnose.** The log line carries `app`, `source_topic`, and `token` — trace that token through the mover
logs to the root failure (a bad payload, a persistent downstream outage).

**Act.** Fix the root cause, then **replay** from the retained JetStream stream (or re-trigger the stage).
Deliberately no auto-requeue — re-firing a poison message blind would loop the cascade.

## Outbox not draining

**Symptom.** `outbox_depth` sustained > 0 and `outbox_oldest_age` climbing (the "alertable pair" panel).

**Cause.** The lineage outbox stages each event durably before publishing; the relay drains it. A sustained
non-zero depth means the relay isn't draining — usually the lineage service or NATS is unhealthy.

**Diagnose.** Lineage pod health + logs; NATS health. A healthy relay drives depth back toward 0.

**Act.** Restore lineage / the bus; the relay re-ingests staged survivors idempotently on recovery.

## RustFS down → data plane

**Symptom.** Catalog reads/writes fail; Lance datasets unreadable; GreptimeDB flush errors.

**Cause.** `rustfs` is a single-replica object store backing BOTH the Lance lakehouse and GreptimeDB's
object storage — a data-plane SPOF (kind). Prod answer is a managed S3 (`rustfs.externalEndpoint` — see
[DECISIONS.md "P4/P7"](DECISIONS.md)).

**Diagnose.** `kubectl get pods -l app.kubernetes.io/component=rustfs`; check the PVC is bound and the node
can mount it (RWO + Recreate strategy means it can't move nodes while the old pod holds the volume).

**Act.** Recover the pod/volume; for prod durability, externalize to managed S3.

## AGE Postgres down → lineage + FGA

**Symptom.** Lineage ingest stalls, `/runs` errors, AND authz fails — because this one pod is both the
lineage graph store and OpenFGA's datastore.

**Cause.** Single StatefulSet replica (kind), a dual-purpose SPOF. Prod answer is managed Postgres
(`age.externalHost` / CloudNativePG — see [DECISIONS.md "P4/P7"](DECISIONS.md) and docs/CNPG-AGE.md) —
sized via the `resources.age` tier (1Gi in prod).

**Diagnose.** `kubectl get pods -l app.kubernetes.io/component=age-postgres`; `pg_isready`; PVC bound?

**Act.** Recover the pod/volume; lineage self-bootstraps the graph on reconnect (`ensure_graph()`). For
prod, externalize to a replicated managed Postgres and take real backups
([RUNBOOK-restore.md](RUNBOOK-restore.md)).
