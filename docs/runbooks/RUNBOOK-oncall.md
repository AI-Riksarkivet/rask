# On-call runbook

Symptom → cause → action for the known failure modes of the Lance-lakehouse estate. Grounded in how this
stack actually fails. The proven rules (`deploy/alerting/rules.yml`) page through the platform's evaluator; in
development the observability fixture's vmalert → Alertmanager evaluates them — note the live vmalert→GreptimeDB
round-trip is still an unrehearsed drill (see [docs/adr/0032-p3b-alerting-rule-logic-proven-hermetically-the-live.md "P3b"](../adr/0032-p3b-alerting-rule-logic-proven-hermetically-the-live.md)). Without an evaluator,
these are found by watching the Perses dashboards (`make dashboards`) or a user report — not a page.

## Orientation

- **Services** (FastAPI): `catalog` (REST catalog, tier governance and the model registry), `lineage`
  (OpenLineage intake into the commit feed and edge list, Lance tables in the catalog's `provenance` namespace;
  the durable `GET /events` log; the relay tick that drains the outbox and forwards to the graph backend), `maintenance` (planner + worker),
  `notifications`, `flows`, and the zones (`web-<zone>`); public `/api/*` reaches them through the platform's Gateway on the chart's HTTPRoutes. The lakehouse runs no stage and no pipeline: moving data
  between tiers is the user's job on their own compute ([ADR 0155](../adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)).
- **Stores**: `age-postgres` (OpenFGA's datastore **AND** the Dapr state store — one pod, dual-purpose),
  the object store (RustFS in development: every Lance table, lineage's provenance tables included, and the registries), `openfga` (authz), the secret store (the openbao
  fixture in development; the platform's Vault or OpenBao in production), `greptimedb` (metrics), `dex` (OIDC).
- **Signals**: Perses panels (`make dashboards`), `kubectl logs`, and the domain metrics —
  `lineage_events_processed_total{lance_lineage_outcome}` (incl. `DEAD_LETTERED`),
  `outbox_depth` / `outbox_oldest_age_seconds`.
- **First move for any pod issue**: `kubectl get pods -o wide`, then `kubectl logs <pod> --all-containers
  --tail=100` and `kubectl describe pod <pod>` (Events section).

## Symptom index

| Symptom | Jump to |
|---|---|
| Everything returns 503 / auth failures across the board | [OpenFGA down](#openfga-down--503-everywhere) |
| App pods stuck `0/1` "waiting for application startup" after a restart/drain | [The secret store sealed](#the-secret-store-sealed--boot-deadlock) |
| A pod `CrashLoopBackOff` shortly after boot | [Slow boot / startupProbe](#crashloop-on-boot) |
| lineage `/readyz` = 503 `{"catalog":"unavailable"}`, pod `NotReady` | [Lineage not ready](#lineage-not-ready--catalog-unreachable) |
| `dapr_dead_letter_parked` ERROR logs | [DLQ parking](#dlq-parking--a-delivery-gave-up) |
| `LineageSignatureRefusedForAListedSigner` firing, `lineage_signature_refused` WARN logs | [Lineage refusing a signer's events](#lineage-refusing-a-signers-events) |
| `outbox_depth` sustained > 0, `outbox_oldest_age_seconds` climbing | [Outbox not draining](#outbox-not-draining) |
| Catalog reads/writes fail, Lance data unreachable | [Object store down](#object-store-down--data-plane) |
| FGA failing, notifications' inbox state unreadable | [AGE down](#age-postgres-down--fga--dapr-state) |
| `DaprWorkflowHistoryNotCollected` / `DaprWorkflowStateMetricsMissing` firing | [Workflow history not collected](#workflow-history-not-collected) |

---

## OpenFGA down → 503 everywhere

**Symptom.** Broad 503s: catalog writes, lineage reads AND notifications all fail at once. Logs show
`authorization service is not available` or FGA check timeouts.

**Cause.** OpenFGA is the authz chokepoint — every governed call `fga.check`s it and **fails closed**. A
single-replica OpenFGA (kind) that is drained/OOMed/rescheduling takes the whole estate down until it
returns. The prod overlay runs 3 replicas + a PDB, which makes this a quorum event, not a total outage.

**Diagnose.** `kubectl get pods -l app.kubernetes.io/name=openfga`; check its datastore (`age-postgres`) is
up — OpenFGA stores tuples in the `rask-age` Postgres, beside the Dapr state store.

**Act.** Wait for reschedule / scale it back up; if the datastore is the real cause, see
[AGE down](#age-postgres-down--fga--dapr-state). Never "fix" a fail-closed 503 by disabling auth in prod.

## The secret store sealed → boot deadlock

**Symptom.** After a node drain / pod restart, app pods hang `0/1` at "waiting for application startup"; the
startupProbe eventually restarts them, looping.

**Cause.** The platform's store (`openbao.externalAddr`) is sealed or unreachable. Apps consume secrets fail-closed
via Dapr **at startup** (the ~80s secret fetch), so while the store is sealed every app pod blocks on boot — the
documented two-sided deadlock. Unsealing it, and auto-unseal, are the platform's ([[XC-149]]); the dev OpenBao
fixture is in memory and never sealed.

**Diagnose.** The store's status from the platform (`bao status` or `vault status` against `openbao.externalAddr`)
→ `Sealed: true`.

**Act.** The platform unseals its store, then the app pods complete boot on their next startupProbe cycle (no
manual app restart needed — the 300s budget covers it).

## CrashLoop on boot

**Symptom.** An app pod restarts repeatedly shortly after scheduling; logs show the lifespan never finishing.

**Cause.** The FastAPI lifespan (Dapr secret fetch ~80s worst case + FGA provision; on lineage, creating its
provenance tables through the catalog, retried until the catalog answers) runs **before** uvicorn serves. The
`startupProbe` gives 300s for this; if boot exceeds it, a dependency is genuinely stuck — usually the secret store sealed
(above), OpenFGA unreachable, or, for lineage, the catalog (`lineage_provenance_tables_not_ready` WARN lines).

**Diagnose.** `kubectl logs <pod> --previous` for where the lifespan stalls; check OpenBao, OpenFGA and the catalog.

**Act.** Fix the upstream dependency; the pod recovers on its own once boot can complete.

## Lineage not ready → catalog unreachable

**Symptom.** Lineage's `/readyz` returns 503 with `{"catalog":"unavailable"}`; the pod goes `NotReady` and is
pulled from rotation, and `readyz_catalog_unavailable` WARN lines appear.

**Cause.** Lineage keeps no database: its commit feed and edge list are Lance tables in the catalog's
`provenance` namespace, read and written through the catalog's doors as lineage's own principal. `/readyz`
gates on the feed table answering through the catalog within 2 s. While it cannot, every delivery answers
`RETRY`, so the bus keeps the events and nothing is lost.

**Diagnose.** Catalog pod health and logs; whether the catalog created the provenance namespace at boot (it
retries until it holds); a 403 from the catalog means lineage's subject is not among `LANCE_PROVENANCE_WRITERS`
or its projected `rask-catalog` token (`RASK_CATALOG_IDENTITY_TOKEN_FILE`) is not mounted.

**Act.** Restore the catalog; lineage returns to Ready on its own and the bus redelivers.

## DLQ parking → a delivery gave up

**Symptom.** `dapr_dead_letter_parked` ERROR logs (and `LineageIngestDeadLettering` or `NotificationsDeadLettering`
for those subscribers). A specific event was not processed after exhausting its retry schedule.

**Cause.** Dapr's Resiliency retry policy was exhausted for a delivery, so the sidecar published it to the
DLQ topic and acked the original (no infinite retry loop). The message is retained in JetStream.

**Diagnose.** The log line carries `app`, `source_topic`, and `token` — trace that token through the subscribing
service's logs to the root failure (a bad payload, a persistent downstream outage).

**Act.** Fix the root cause, then **replay** from the retained JetStream stream. Deliberately no auto-requeue —
re-firing a poison message blind would loop it through the same failure.

## Lineage refusing a signer's events

**Symptom.** `LineageSignatureRefusedForAListedSigner` fires, with `lineage_signature_refused_total{lance_lineage_reason}`
rising for a reason other than `unsigned` or `signer`, and `lineage_signature_refused` WARN lines on the lineage pod.

**Cause.** Where `LINEAGE_SIGNERS` is rendered, lineage records a bus or staged event only when a listed signer's
published Ed25519 key verifies its `rask_signature`. A refusal is **acked**: it is not retried, not parked in the DLQ and
never recorded, so the counter and the log line are the only trace. By reason: `kid` — the signer signed with a key its
`signing-public-<identity>` secret does not list (a store that was re-minted under signers that kept the old key, or an
identity that never published); `signature` — the bytes that arrived differ from the bytes signed; `author` / `delegation`
— stamped as a subject the signer may not act for; `uncanonical` / `encoding` / `error` — the event cannot be put in its
canonical form or its signature is not canonical base64url. `unsigned` and `signer` are counted but do not page: no
genuine signer produces either.

**Diagnose.** The log line carries `run_id` (or `dataset` for a catalog change), the claimed `identity` and the `reason`.
Compare the signer's key id with the keys its identity publishes: `GET /v1.0/secrets/lance-secrets/signing-public-<identity>`
through any sidecar allowed to read it, field `keys`, current key first. An outage reads differently: the key list could not
be read, so lineage answers RETRY (`lineage_signature_keys_unavailable`) and refuses nothing.

**Act.** A signer whose key is not listed: restart it so it re-resolves its key, and after a store re-mint restart every
signer. Do not add a public key to a list to make events pass unless you know whose key it is; an identity that is being
forged is an incident, and the response is to rotate that identity's key. An event already refused was acked and is not
presented again; it stays on the retained stream (168 h), so a rebuild that re-reads the stream verifies it afresh against
the keys published by then.

## A bus door refusing events

**Symptom.** `NotificationsDoorRefusingEvents` or `MaintenanceArrivalRefusingEvents`
fires, with `<service>_signature_refused_total` rising for a door and a reason, and `<service>_signature_refused` WARN
lines on that service's pods naming the door, the claimed identity, the event and the reason. Every door x reason
series exists at 0 from the pod's start, so one refusal fires its alert about two minutes later, and the alert resolves
about three minutes after that unless another refusal follows. The catalog's own `/control-events` door counts its
refusals in `catalog_signature_refused_total` with a `catalog_signature_refused` WARN line, and no alert reads it.

**Cause.** With `RASK_SIGNATURE_DOORS=enforce`,
notifications (`/lineage-events`, `/control-events`), the catalog's `/control-events` and maintenance's `/maintenance-arrival` act on a bus event only when
a signer that door allows verifiably signed it: a lineage event against `RASK_EVENT_SIGNERS` and `RASK_EVENT_DELEGATORS`,
a control event against the identities `RASK_CONTROL_SIGNER_ROLES` lists for the role
`service_kit.control_events.control_signer_role` names for its action. A refusal is **acked**: the event drove nothing,
is not retried and is not parked. The reasons read as in § Lineage refusing a signer's events, with two differences. At a
control door, `signer` also means a listed identity signed an action its role does not emit. And here `unsigned` and
`signer` page: every bus publisher signs and no action is exempt, so an unsigned or unlisted event on the bus is a
forgery or a producer that stopped signing.

**Diagnose.** Read the claimed identity off the log line and compare it with the door's lists in the pod's environment.
An identity a list lacks after a chart change is a render defect. `kid` or `signature` from a listed identity reads as
§ Lineage refusing a signer's events. A key list that could not be read is not a refusal: the door answers RETRY and logs
`<service>_signature_keys_unavailable`.

**Act.** A forged event is an incident: find the pod that published it, and rotate any identity whose key it presented.
A signer that broke: restart it so it re-resolves its key. A refused event is not presented again; a write maintenance
refused to plan is picked up by the hourly sweep.

## Outbox not draining

**Symptom.** `outbox_depth` sustained > 0 and `outbox_oldest_age_seconds` climbing (the "alertable pair" panel).

**Cause.** The lineage outbox stages each event durably before publishing; the relay drains it. A sustained
non-zero depth means the relay isn't draining — usually the lineage service or NATS is unhealthy.

**Diagnose.** Lineage pod health + logs; NATS health. A healthy relay drives depth back toward 0. On the catalog's
control lane (`service.name` catalog), `control_relay_keys_unavailable` lines mean its sidecar cannot read
`signing-public-<catalog identity>`, so nothing staged is delivered until it can; `control_outbox_unverifiable` lines
mean something other than the catalog wrote under `_control_outbox`: treat that as a forgery
(§ A bus door refusing events).

**Act.** Restore lineage / the bus; the relay re-ingests staged survivors idempotently on recovery.

## Object store down → data plane

**Symptom.** Catalog reads/writes fail; Lance datasets unreadable.

**Cause.** In development the object store is the `rustfs` fixture (`deploy/fixtures/rustfs`, StatefulSet
`rask-rustfs`), a data-plane single point of failure. The production answer is the platform's managed S3
(`objectStore.externalEndpoint` — see
[docs/adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md "P4/P7"](../adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md)).

**Diagnose.** `kubectl get pods -l app.kubernetes.io/component=rustfs`; check its claim
(`data-rask-rustfs-0`) is bound and the node can mount it. The pod is Ready only when `/health/ready` answers on
:9000; liveness answers during startup while every S3 request still gets 503, so judge "back" by readiness.

**Act.** Recover the pod/volume; for prod durability, use the platform's managed S3.

## AGE Postgres down → FGA + Dapr state

**Symptom.** Authz fails across the estate (lineage's ingest gate answers `RETRY`, so events stay on the bus)
AND notifications cannot read or write inbox state — because this one pod is both OpenFGA's datastore and the
Dapr state store.

**Cause.** Single StatefulSet replica (kind), a dual-purpose SPOF. Prod answer is managed Postgres
(`age.externalHost` / CloudNativePG — see [docs/adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md "P4/P7"](../adr/0033-p4-p7-backups-structural-spofs-the-prod-answer-is.md) and docs/CNPG-AGE.md) —
sized via the `resources.age` tier (1Gi in prod).

**Diagnose.** `kubectl get pods -l app.kubernetes.io/component=age-postgres`; `pg_isready`; PVC bound?

**Act.** Recover the pod/volume; OpenFGA and the sidecars reconnect on their own. For
prod, externalize to a replicated managed Postgres and take real backups
([RUNBOOK-restore.md](RUNBOOK-restore.md)).

## Workflow history not collected

**Symptom.** `DaprWorkflowHistoryNotCollected` — the oldest workflow-history row in the Dapr state store
is older than the longest configured retention window plus its collection margin. Or
`DaprWorkflowStateMetricsMissing`, which means the measurement itself has stopped and the first alert
can no longer fire at all.

**Cause.** Workflow history is append-only and `spec.workflow.stateRetentionPolicy` on the `lance-tracing`
Configuration is the ONLY thing that removes it. There is deliberately no application-side purge to fall
back on: `DaprWorkflowClient` exposes `purge_workflow(instance_id)` but no list-instances API, so after a
restart nothing can name the instances whose history persists. Three ways it breaks — the policy dropped by
a values edit (`dapr.workflowRetention.enabled`), the `dapr.io/config` annotation re-gated so sidecars stop
referencing the Configuration at all (it was once gated on `otelEnabled`, which silently took retention
down with telemetry), or the Dapr scheduler failing to collect.

**Diagnose.**

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# 1. is the policy live, and what does it say
kubectl get configuration.dapr.io lance-tracing -o jsonpath='{.spec.workflow}'

# 2. does every workflow-hosting sidecar still reference that Configuration
kubectl get pods -o custom-columns='POD:.metadata.name,CFG:.metadata.annotations.dapr\.io/config' --no-headers

# 3. what the alert is reading — volume, and the age spread by day
kubectl exec rask-age-0 -- bash -lc "psql -U lance -d daprstate -tAc \
  \"select count(*), min(insertdate)::date, max(insertdate)::date from state where key like '%workflow%'\""
```

`count(expiredate) = 0` across the whole table is **expected and not a symptom** — the scheduler enforces
retention by DELETING rows, not by writing a TTL column. Judge collection by the age distribution, never by
that count.

For `DaprWorkflowStateMetricsMissing`, the target is the Collector, not Dapr: check that the
`sqlquery/daprstate` receiver is still in `chart/templates/otel-collector.yaml`, that the collector pod is
running, and that it can reach AGE on 5432 (under `networkPolicy.enabled` the `otel-collector` component
must be in the age-postgres ingress allow-list).

**Act.** Restore the policy, then purge what accumulated while it was off — the policy applies only to
workflows that *newly* reach a terminal state, so it cannot reach backwards and the backlog stays forever
otherwise. Purge through the sidecar, never with a direct `DELETE` on `daprstate.state`, which bypasses the
actor runtime that owns those keys. `flows` is the one service that hosts Dapr workflows, so its sidecar is the
one that can purge them:

```bash
POD=$(kubectl get pod --no-headers -o name | grep rask-flows | head -1)
IDS=$(kubectl exec rask-age-0 -- bash -lc "psql -U lance -d daprstate -tAc \
  \"select distinct split_part(key,'||',3) from state \
    where key like '%workflow%' and insertdate < '<cutoff-date>'\"")
for id in $IDS; do
  kubectl exec ${POD#pod/} -c flows -- \
    curl -s -X POST "http://127.0.0.1:3500/v1.0-beta1/workflows/dapr/$id/purge"
done
```

Two things that bite here. The distinct ids are **not** all instances — activity records are keyed
`<instance-uuid>:0004`, so a loop that assumes one id per instance purges the wrong set (purging the real
instances collects their activity keys anyway). And purge requires a TERMINAL instance; a COMPLETED one
accepts it with no `--force` involved.
