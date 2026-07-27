# lance-ns — how it all works (event-driven on kind)

A Lance lakehouse REST catalog + in-service lineage (OpenLineage → Apache AGE) + governance, running as
**event-driven microservices on a local kind cluster**, deployed by one umbrella Helm chart and
iterated with Tilt. Diagram: [`k8s-event-driven-architecture.html`](k8s-event-driven-architecture.html).

## One command

```bash
make up        # bootstrap toolchain + kind cluster + build images + deploy everything
make verify    # prove the event-driven flow end-to-end
make dashboards# port-forward the UIs (web / lineage / Perses / GreptimeDB / Dapr dashboard)
make k9s       # inspect the cluster
make tilt-up   # dev loop: hot-reload the FastAPI services
make down      # tear down
```

`make up` is idempotent. It needs only **docker** + **helm** on PATH; it downloads kind/kubectl/k9s/tilt
into `.localbin/` (gitignored).

## The components (one `helm install`, all gated by `<key>.enabled`)

| Component | Kind | Role |
| --------- | ---- | ---- |
| **catalog** | app (FastAPI) + daprd sidecar | **producer** — creates Lance tables on S3; publishes OpenLineage events via Dapr |
| **lineage** | app (FastAPI) + daprd sidecar | **consumer** — Dapr subscription ingests events into Apache AGE; serves the lineage API |
| **frontend zones** | 4 apps (SvelteKit SSR) | the UI as rask-style micro-frontend **zones** — `home` (catch-all `/`, owns `/auth/*`) + `lakehouse` (`/lakehouse`) + `media` (`/media`) + `annotator` (`/annotator`). `lakehouse` is one app hosting four AREAS at `/lakehouse/{data,lineage,models,admin}`: they were four separate zones over one backend plane and one shared client, paying four SSR servers and a hard reload per hop for no independent-deploy payoff. `annotator` stays separate from `media` despite sharing its plane, purely to keep its Pixi + OpenCV bundle out of a searcher's. One parametrized `frontend.dockerfile` (`lance-<zone>:tag`, per-zone tag override on `frontend.apps`); the **Ingress** path-routes each zone (`ingress.yaml`); every zone shares one env seam (`lance.frontendEnv`) + reads one origin-wide OIDC session cookie. The admin AREA serves the ops pages `/lakehouse/admin/{audit,dlq,events,streams,tenants,access}` behind a fail-closed estate-admin gate; the data area carries the namespace + table **lifecycle** surfaces. | the UI as rask-style micro-frontend **zones** — `home` (catch-all `/`) + `data` (`/data`) + `lineage` (`/lineage`) + `models` (`/models`) + `admin` (`/admin`). One parametrized `frontend.dockerfile` (`lance-<zone>:tag`); the **Ingress** path-routes each zone (`ingress.yaml`); every zone shares one env seam (`lance.frontendEnv`) + reads one origin-wide OIDC session cookie (the `home` zone owns `/auth/*`). Replaces the retired single `web` pod. The admin zone additionally serves the ops pages `/admin/{audit,dlq,events,streams,tenants}` — the estate-wide ones (events feed, JetStream panel, tenant admin) gate on the `auth.bootstrapAdmin` estate-admin grant — and the data zone carries the namespace + table **lifecycle** surfaces (`/data/{namespaces,tables,warehouses}`: declare/create, drop/deregister/restore, rename, grants). |
| **medallion** | 4 apps + sidecars | event-driven pipeline: `lance-ray` producer + raw→bronze→silver→gold movers (see [MEDALLION.md](MEDALLION.md)) |
| **compaction** | app + sidecar | compaction/GC service triggered by a Dapr **cron binding** (`bindings.cron`) — compacts Lance fragments + GCs old versions |
| **gateway** | nginx + sidecar | **backend** clean-URL edge that routes API traffic via **Dapr service invocation** (`/v1.0/invoke/...`): `/lineage/`,`/catalog/`,`/produce` (values-gated: `medallion.producer.expose`, off in prod — it's the unauthenticated demo entry),`/perses/`,`/greptime/`. Reached by a port-forward to its Service (`make dashboards`); it is **out of the frontend path** now (the zones' BFF proxies reach the backend directly, and the Ingress routes the zones). Dapr-delivered routes (lineage ingest + the reconcile cron binding) are **403-blocked** from one source (`lance.lineageSidecarOnlyRoutes`) — the sidecar is their only legitimate caller |
| **Dapr** | subchart | control plane + sidecar injection + pub/sub + secret-store + tracing config |
| **NATS** | subchart | JetStream — the durable event bus behind Dapr pub/sub |
| **Apache AGE Postgres** | StatefulSet | the lineage graph (`lineage`) **and** OpenFGA's datastore (`openfga` db) |
| **OpenFGA** | subchart | Zanzibar authz (datastore = the AGE Postgres) |
| **Dex** | Deployment | OIDC issuer (authn) |
| **RustFS** | Deployment | S3-compatible object store for the Lance lakehouse |
| **OpenBao** | Deployment | secret store (Vault fork), fronted by a Dapr `secretstores.hashicorp.vault` component |
| **GreptimeDB** | subchart | **one unified store for metrics + logs + traces** (on RustFS S3) — the OTel Collector exports all signals here |
| **OTel Collector** | template | Receives app OTLP + tails infra logs (filelog) + scrapes Dapr metrics → GreptimeDB (`opentelemetry_logs`/`_traces`/metrics) |
| **Perses** | subchart | dashboards-as-code over GreptimeDB's Prometheus API (`/v1/prometheus`) |
| **Dapr dashboard** | Deployment | web UI for the Dapr stuff (components, subscriptions, configurations) |

## The event-driven path (verified)

```
catalog --DaprClient.publish_event--> [daprd sidecar] --pubsub.jetstream--> NATS JetStream (stream LINEAGE)
                                                                                   │
   Apache AGE  <--cypher-- lineage  <--POST /lineage-events-- [daprd sidecar] <-----┘
   (:User)-[:CREATED]->(:Dataset)        (Dapr subscription)
```

- The catalog publishes to its **local** sidecar; the sidecar owns retry/backoff + **W3C
  trace-context propagation** as component config (no broker client in app code; no DLQ —
  [`RESILIENCE.md`](RESILIENCE.md) gap #2).
- The lineage service subscribes via `dapr-ext-fastapi` (`/dapr/subscribe` → `/lineage-events`),
  ingests into AGE, and returns the Dapr ack status (`SUCCESS`/`RETRY`/`DROP`). Ingest is idempotent
  (MERGE on `run_id`).
- **Every subscriber has its own pubsub component** (`lineage-pubsub-<app-id>`, the `lance.subPubsub`
  helper): `queueGroupName=<app-id>` makes that app's replicas a competing-consumer group (single
  delivery per app — safe to scale), and `deliverPolicy` is `all` for lineage (restart replays into the
  idempotent MERGE = the durability story) but `new` for the cascade head + movers (a replay would
  re-fire every cascade in the 168h retention window). The bare `lineage-pubsub` component is publish-only
  (catalog + compaction).
- `make verify` publishes a `create_table` event and asserts AGE recorded the creator.

## Observability (OTel Collector + GreptimeDB + Perses — the rask stack)

All three OTel signals are **valuable and queryable**, mirroring rask: the apps run under
`opentelemetry-instrument` and export **plain OTLP to a single in-chart OTel Collector**
(`observability.otelCollector`, `chart/templates/otel-collector.yaml`) — the one telemetry hub. The
Collector receives the app OTLP, `filelog`-tails the no-SDK infra-pod logs, and scrapes the Dapr
sidecars' `:9090` via its `prometheus` receiver, then exports all three signals OTLP → GreptimeDB
(adding the `x-greptime-*` db-name/pipeline headers the apps no longer carry). GreptimeDB is one unified
store; Perses dashboards it over GreptimeDB's Prometheus-compatible API. Verified end-to-end
(`make e2e-obs`):

- **Traces** → `opentelemetry_traces`. A **single distributed trace spans catalog → Dapr publish →
  lineage → AGE write** (`opentelemetry-instrumentation-grpc` injects `traceparent` into the gRPC
  publish; the lineage FastAPI extracts it). Dapr's `lance-tracing` Configuration keeps `samplingRate=1`
  so the W3C context always propagates — it has **no** otel exporter (Dapr's own spans can't carry
  GreptimeDB's required headers; the apps do all export).
- **Metrics** → PromQL. Custom **domain** golden signals `lineage_events_processed_total{lance_lineage_outcome}` +
  `lineage_ingest_duration_seconds_*` (recorded in the consumer), plus FastAPI RED
  (`http_server_duration_milliseconds_*`). Export interval is 5s (`OTEL_METRIC_EXPORT_INTERVAL`).
- **Logs** → one table, `opentelemetry_logs`: the apps export OTLP logs directly, and the Collector's
  `filelog` receiver tails the no-SDK infra pods into the **same** table (Vector is gone — there is no
  more `lance_logs`).

`make e2e-obs` runs `tests/e2e/test_observability_e2e.py` — one catalog table-create, then it asserts the
graph data landed in AGE, the metric incremented in PromQL, the distributed trace joined catalog+lineage,
and both the app and infra log paths landed in `opentelemetry_logs`. That's the regression guard for "the
pipeline works **and** is observable".

**Lance-native IO metrics (pre-wired, activates at pylance 9):** pylance ≥ 9.0 ships
`lance.otel.instrument_lance_metrics()` (the `pylance[otel]` extra) — Lance's Rust object-store/IO
metrics registered straight onto the global MeterProvider the services already run under. Every
Lance-I/O service (catalog, lineage, compaction, medallion producer + movers) already calls the guarded
`common.lance_metrics.instrument_lance_if_available()` at startup; at the pinned pylance 8.0.0 it logs
`lance_metrics_unavailable` and no-ops. When 9.0 ships on PyPI (8.0.0 is the newest as of 2026-07-10),
switch the pin to `pylance[otel]` (marked in `pyproject.toml`) and the metrics appear in GreptimeDB with
no further code. OTel has no async histogram instrument, so Lance histograms arrive Prometheus-style as
`<name>_bucket`/`_count`/`_sum` counters with `le` attributes.

## Governance (Dex + OpenFGA)

Deployed; `auth.enabled=false` by default. Set `auth.enabled=true` to wire OIDC (Dex) verification +
OpenFGA `can_get_metadata` checks into the catalog + lineage. The OIDC `ALLOW_INSECURE` escape hatch is
SCHEME-DERIVED from the issuer (2026-07-12): a plain-http issuer (the in-cluster dev Dex) opens it, an
https issuer (any real IdP) keeps the verifier's HTTPS guard enforced — never hardcoded open. OpenFGA's schema migrates against the AGE
Postgres (pinned to v1.8.0; the openfga db's `search_path` is forced off AGE's `ag_catalog`).

**First estate admin (`auth.bootstrapAdmin`).** Set it to a Dex subject (the id_token's `sub`, e.g.
`alice`) and a post-install/upgrade hook Job grants that user `owner` on the FGA root object
(`warehouse:lance_catalog`) — the grant the estate-wide surfaces gate on (`GET /v1/events`, the admin
console's events/streams/tenants pages). Empty (the default) renders no Job and grants stay out-of-band
(seed scripts / the UI). Requires `auth.enabled`; the Job is idempotent (check-before-write).

**Per-user UI login across the zones (opt-in `frontend.oidc.enabled`).** By default the zones run auth-OFF
even when the backends are governed: each reads lineage as `frontend.serviceIdentity` (allow-listed in
`LINEAGE_SERVICE_SUBJECTS`), and catalog control-plane surfaces show "sign in" with no way to sign in —
because per-user OIDC login needs a **browser-reachable IdP**, which the in-cluster `dex.issuer` is not. Turn
it on with `frontend.oidc.enabled=true` + `frontend.oidc.publicIssuer` (the external Dex/Keycloak/Auth0
issuer) + `frontend.oidc.publicOrigin` (the browser origin that forms the `…/auth/callback` redirect) +
`frontend.oidc.sessionSecret` (≥32 chars; seals the session cookie AES-256-GCM via a Secret — required,
render fails without it).

**Cross-zone login is one origin, one cookie.** The `home` zone owns `/auth/{login,callback,logout}`; the
sealed session cookie is set at path `/`, so because the Ingress path-routes every zone under one origin, a
login on `data` is a login on `admin` too. Every zone's `hooks.server.ts` reads the same cookie
(`makeSessionHandle`) and its BFF forwards the signed-in user's OWN bearer to the governed backends (per-user
authz). The BFF is a **confidential** OIDC client — the bundled `dex.clientId` carries a secret, wired as
`OIDC_CLIENT_SECRET` from `dex.clientSecret` via the `<release>-frontend-session` Secret (secretKeyRef,
zero-plaintext); the bundled Dex registers the zones' `…/auth/callback`. NOTE: the audit viewer's admin gate
calls `MEDALLION_API`, so keep `medallion.enabled=true` when `frontend.oidc.enabled`.

**Proving it live on kind (the cross-zone drive).** The composition needs the Ingress, so kind needs an
Ingress controller. These steps mutate the cluster — run them yourself (or `!`-prefix each):

```bash
# 1. build + side-load the 4 zone images into kind
!make frontend-images && make frontend-load

# 2. one-time: an Ingress controller on kind (ingress-nginx)
!kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.2/deploy/static/provider/kind/deploy.yaml
!kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=180s

# 3. deploy governed + zones OIDC-on + ingress-on (publicOrigin = the forwarded ingress origin;
#    produceAdminProject=acme so alice's admin grant opens the produce door)
!helm upgrade --install lance-ns ./chart --timeout 300s \
   --set auth.enabled=true --set auth.bootstrapAdmin=alice \
   --set medallion.enabled=true --set medallion.produceAdminProject=acme \
   --set ingress.enabled=true \
   --set frontend.oidc.enabled=true \
   --set frontend.oidc.publicIssuer=http://lance-ns-dex:5556/dex \
   --set frontend.oidc.publicOrigin=http://localhost:8090 \
   --set frontend.oidc.sessionSecret=$(head -c48 /dev/urandom | base64 | tr -d '/+=' | head -c48)
!kubectl rollout restart deploy/lance-ns-dex deploy/lance-ns-lance-ray

# 4. drive the cross-zone login + authz (the script does its own ingress/dex/openfga port-forwards +
#    seeds alice=admin project:acme, then runs the headless browser through the ingress origin)
!bash scripts/verify_cross_zone_oidc.sh
#    → alice signs in on /lakehouse/data → still signed-in on /media (one origin, shared cookie —
#      it must cross a real ZONE boundary to prove anything); alice 2xx / bob 403
```

**Proven live on kind (2026-07-22):** all 5 zones rolled out Ready; the Ingress path-routed each zone
(`/`→home 200, `/data`/`/lineage`/`/models`/`/admin`→their zone SSR); `verify_cross_zone_oidc.sh` drove a
real Dex login — alice signed in on `/data` was still signed in on `/admin` (the shared origin-wide cookie),
her cascade opened the produce door (2xx, run token), and bob was 403-denied (`needs the project-admin
rung`). Cross-zone OIDC + per-user authz proven end-to-end.

`scripts/verify_cross_zone_oidc.sh` drives a real headless Dex login through the Ingress origin and asserts
the shared cookie carries across zones + per-user authz. The browser↔Dex reachability puzzle (the
issuer-derived authorize URL `http://lance-ns-dex:5556/dex/auth` isn't host-resolvable) is solved with
chromium `--host-resolver-rules` mapping `lance-ns-dex:5556` to the Dex port-forward, while
`frontend.oidc.publicIssuer` stays the in-cluster URL so the forwarded bearer's `iss` still verifies at
catalog/lineage.

## Notable engineering notes

- **Dapr JetStream consumers** are split BY RECOVERY STORY (2026-07-06/12): the LINEAGE ingest stays
  **ephemeral** (deliverPolicy=all — a restart replays the retained stream into the idempotent MERGE;
  a durable cursor would defeat that recovery), while the cascade head + movers pair
  `deliverPolicy=new` with a **durable + queue-group** consumer (cursor survives pod death/redeploys —
  chaos-verified; the durable-orphan failure applies only to durables WITHOUT a queue group). Since
  2026-07-12 the **Dapr Resiliency + DLQ layer is DEFAULT ON** (`dapr.resiliency.enabled`): the sidecar
  owns delivery retries (30s→300s ×5) and exhaustion PARKS the message on the per-app `dlq.*` topic
  (own DLQ stream) — see docs/RESILIENCE.md gap #2. (The once-planned durable PULL consumer move was
  retired 2026-07-12 by the Dapr-first rule — RESILIENCE.md records the ruling; the remaining
  resilience residuals are in OPEN-WORK.md E2.)
- **Upgrade caveat: a consumer-config-changing upgrade silently stalls durable subscriptions for up
  to ~25 min** (observed live 2026-07-13). JetStream durables are create-once, so after an upgrade
  that changes the consumer config (e.g. the resiliency/DLQ default-ON maxDeliver/backOff change)
  the sidecars can't bind the old `<app>-durable` consumers — pods stay Ready but nothing is
  delivered until JetStream reaps the old durables at their inactive threshold (~20–25 min), after
  which delivery resumes on its own. The chart's stream-provision Job now reconciles this automatically at
  every `helm upgrade` (drifted `*-durable` consumers are deleted; sidecars recreate them within
  seconds); manual fast cutover — `nats consumer rm` the `<app>-durable` consumers on
  LINEAGE/MEDALLION/TRAINING/DLQ — is only needed when upgrading with a chart older than that Job.
  Fresh installs never hit this. Details: [`RESILIENCE.md`](RESILIENCE.md) gap #7.
- **Control-plane change-events (`catalog.controlEmit`, default on):** the catalog broadcasts its own
  control-plane mutations (create/drop/grant/…) on a **dedicated group-less pubsub component**
  (`pubsub.controlName` = `catalog-control-pubsub`, topic `catalog.control.v1`, own `CATALOG_CONTROL`
  stream) so every catalog replica buffers every event; clients poll `GET /v1/events` (estate-admin
  gated) and the admin zone renders it at `/admin/events`. Best-effort + fail-open: a bus outage
  degrades to "no live refresh" (the audit trail still records the mutation), never a failed mutation.
  The per-replica ring buffer is correct at `services.catalog.replicas=1` (the default).
- **JetStream visibility (`/admin/streams`):** the admin zone's BFF reads the NATS HTTP monitor
  (`NATS_MONITOR_API` → the headless Service's `:8222`, admitted by the `nats-monitor` NetworkPolicy
  from web-admin pods only — the browser never touches NATS) and diffs live consumers against
  `JETSTREAM_EXPECTED_CONSUMERS` (rendered from the same values the subscriptions render from) to
  flag silently-dead subscriptions.
- **Per-tenant medallion routing (`medallion.projectsEnabled`, opt-in, #84):** when true, the producer +
  movers resolve a `project`-carrying trigger to that project's ACTIVE warehouse bucket (via
  `MEDALLION_CONTROL_ROOT`) and the cascade writes `s3://<project-bucket>/medallion/<stage>` with
  project-qualified lineage; project-less traffic is byte-identical, and with it false a project-carrying
  trigger is dropped fail-closed. Warehouse-create refuses the medallion zone buckets
  (`LANCE_RESERVED_BUCKETS`, auto-derived from `medallion.buckets`). Tenant lifecycle is driven from
  `/admin/tenants`.
- **AGE + OpenFGA share one Postgres**: AGE's `ag_catalog` search-path would break OpenFGA migrations,
  so the openfga db is pinned to `search_path = public` in the AGE initdb.
- **kind has no host ports** — reach services via `make dashboards` (port-forwards).

## Status (audited)

✅ Verified: the event-driven catalog→lineage flow, all components healthy (`helm STATUS: deployed`),
Dapr sidecar injection, the full 3-signal observability (`make e2e-obs` green — AGE data + PromQL metric
+ distributed trace + logs in GreptimeDB), `tilt ci` brings the whole stack up green.
✅ Verified: the event-driven **medallion** cascade — one `lance-ray` `/produce` cascades
raw→bronze→silver→gold via Dapr pub/sub, building the lineage DAG, as **one distributed trace** across
all 5 services, with the `medallion_stage_transitions_total` metric in PromQL (`make e2e-medallion`).
✅ Verified: the **compaction/GC** service — a Dapr `bindings.cron` component POSTs `/compaction-cron`
on its schedule (and `make compaction` on demand); each sweep discovers every Lance dataset in the bucket
and runs `compact_files()` + `cleanup_old_versions()`, with `compaction_*` metrics in PromQL.
✅ Verified: the **API gateway** — one nginx front routes `/lineage/*` and `/catalog/*` through its own
Dapr sidecar via **service invocation** (mTLS + retries + tracing on the hop), `/`→web UI; one
port-forward fronts the whole platform (`/lineage/livez` → 200 through the gateway).
✅ Verified: **OpenBao secret consumption** — apps read secrets through their Dapr sidecar
(`GET /v1.0/secrets/lance-secrets/lance` → 200) instead of plaintext env. Fixed the kv-v2 nuance:
Dapr defaults `vaultKVPrefix=dapr` (reads `secret/data/dapr/<key>`); set `vaultKVUsePrefix=false` so it
reads the natural `secret/data/<key>` the seed writes.
✅ Verified: the **Lakekeeper-style multi-tenant ReBAC** — `team → project → warehouse(bucket) →
namespace → table` with concentric `owner>writer>reader` rungs + a `validator` rung that gates medallion
stage promotion. Proven offline (`fga model test`: 7/7 tests, 24/24 checks) **and** live against the
deployed OpenFGA: a plain writer `can_promote` gold = **false**, a validator = **true**; projects are
isolated (one bucket per project; a team can own many). See `services/common/auth/model.fga` + `model.fga.yaml`.
⚠️ Deployed-not-wired: the app auto-seeding of the project/team/warehouse hierarchy on create (the
namespace→warehouse parent + creator-owner are seeded; project/team/validator grants are set out-of-band
for now); the end-to-end Dex-token → catalog → OpenFGA request demo (auth is `--set auth.enabled=true`).
✅ Built: RustFS-native scoped STS via `vending.mode=web_identity` (Dex id_token →
`AssumeRoleWithWebIdentity` + inline per-table session policy). Plain `AssumeRole` STS (`vending.mode=sts`)
works on AWS/MinIO/Ceph but NOT RustFS (it rejects plain AssumeRole — that's why web_identity exists).
