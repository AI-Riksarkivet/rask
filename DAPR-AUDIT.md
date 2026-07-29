# Dapr audit — `rask` @ `baf4494` (branch `feat/lance-ns-merge`), 2026-07-29

## 1. Verdict

**Nothing was lost in the merge.** `git diff 908e047 HEAD -- chart/` is empty — the chart is byte-identical from the lance-ns tip through the merge to HEAD, and `908e047^` (pre-merge rask) had 24 chart files and **zero** `dapr-*.yaml`, so the entire Dapr topology arrived whole from lance-ns and no Component, Resiliency, Configuration or injector label stopped rendering.

**The plane is partially wired, and the defects are gating and ownership, not lost code.** Four of the gateway's eight Dapr invoke targets have no pod at chart defaults; Tilt turns the media plane *on* while the Helm-owned Resiliency CR still believes it is off; the IIIF bronze lane publishes a trigger no declared mover consumes; and one chart-committed retry fix has never been rendered into any release revision.

**What works, works live:** control plane 1/1 on dapr 1.18.1 with mTLS, 12/12 sidecars injected with zero component-init failures, all 19 (component, app-id) scope pairs loading, the OpenBao→Postgres state-store chain proven by schema migration + cleanup timestamps, and per-subject user state round-tripping. What does not work is the **invoke** plane and the **page lane** of the cascade.

---

## 2. Findings

### Majors

---

#### M1 · `compute` app-id has no pod — `/api/ray/*` and `/api/serve/*` are hard-500 on every shipped deploy path

The gateway declares app-id `compute` **unconditionally** (`services/gateway/src/gateway/__init__.py:78`, consumed at `:103` `/api/ray` and `:105` `/api/serve`), but the compute Deployment renders only under `singleTenant.enabled`:

```
chart/templates/fleet.yaml:12
{{- if and (not (has $name $lakehouse)) (or (eq $name "gateway") $root.Values.singleTenant.enabled) }}
chart/values.yaml:38-39   singleTenant: {enabled: false}
```

Live, from inside `rask-gateway`:

```
GET :8888/api/ray/status  → 500 {"errorCode":"ERR_DIRECT_INVOKE",
  "message":"failed to invoke, id: compute, err: failed to resolve address for
   'compute-dapr.default.svc.cluster.local': ... no such host"}
GET :8888/api/serve/applications → 500 (identical)
GET :8888/healthz → 200
kubectl get deploy,svc | grep compute → only rask-web-compute (the zone)
```

No shipped path sets the toggle: `make k3s-up` (`Makefile:399`) passes only `hfToken`/rustfs creds, `chart/values-prod.yaml` does not set it, `Tiltfile:207` sets `media.enabled` but never `singleTenant`. Aggravators:

- `Makefile:406` prints `API → http://<node-ip>/api/ray/health` as the post-install verification URL — the exact route that 500s.
- `Makefile:202` (`dev-frontends-k3s`) blocks in `until curl -sf http://localhost:8888/api/ray/health` — hangs forever on a default install.
- `Makefile:339` `COMPOSE_IMAGES = gateway compute controlplane` — `k3s-build`/`k3s-import` build and import a compute image the chart never deploys.
- `chart/templates/rayservice.yaml:1` carries the same gate, so no RayService renders either despite `ray.enabled: true` (`chart/values.yaml:857`).
- The consumer ships at defaults: `rask-web-compute` renders unconditionally and `frontend/microfrontends/compute/src/lib/remote/compute.remote.ts:86` calls `rayHealth()`.
- Dapr is only the *error shape*, not the cause: with `dapr.sidecars=false` the httpx path targets `RASK_COMPUTE_URL=http://rask-compute:8804` (`chart/templates/configmap.yaml:23`) — a Service the chart never creates — so it 502s instead.

**Smallest fix:** render the compute Deployment + Service unconditionally, the way `chart/templates/controlplane.yaml` already does (`ray.enabled` is true by default and the image is already built and imported). Do **not** patch `dapr-resiliency.yaml` — it deliberately mirrors the render gates (`chart/templates/dapr-resiliency.yaml:88-93`), and adding a target for a pod that does not exist would be the wrong fix.

---

#### M2 · Tilt runs the media plane; Helm owns a Resiliency CR that does not know it exists

`chart/templates/dapr-resiliency.yaml:109-113` adds viewer/search/annotator to `targets.apps` behind `{{- if .Values.media.enabled }}`. `Tiltfile:166-190` renders a fixed `FLEET_TEMPLATES` list with `--set media.enabled=true` (`Tiltfile:207`) — and **`templates/dapr-resiliency.yaml` is not in that list**. The Resiliency CR therefore still comes from the `make k3s-up` release, rendered at the default `media.enabled: false` (`chart/values.yaml:663`).

```
kubectl get resiliency.dapr.io rask-invocation-resiliency -o jsonpath='{.spec.targets.apps}'
→ {"catalog":…,"controlplane":…,"lance-ray":…,"lineage":…}      # scopes: ["gateway"]
kubectl get deploy rask-viewer -o custom-columns=…managed-by → tilt   (helm release-name: <none>)
helm get manifest rask --revision 15 | grep rask-viewer → (nothing)
```

Invocation is genuinely live: `RASK_DAPR_ENABLED=true` in the gateway pod, `__init__.py:87-97` routes `/api/media*` to viewer/search/annotator, `:152-158` sends them to `http://127.0.0.1:3500/v1.0/invoke/<app-id>/method`, and from inside the gateway `GET :8888/api/media/health → 200` (a bogus app-id control returns 500 `ERR_DIRECT_INVOKE`, so the 200s are real Dapr resolutions).

Consequence: **four** invoked app-ids — viewer, search, annotator **and** compute — get no `invokeTimeout`, no `invokeRetry` (constant 2s ×3) and no `invokeBreaker` (trip at 5 consecutive failures, 30s shed), while the identical call to `catalog` gets all three. Dapr's built-in connection-level `DaprBuiltInServiceRetries` still applies; the gateway's own `httpx.Timeout(30.0, read=300.0)` (`__init__.py:195`) still bounds the call — so "no timeout" is true at the Dapr layer only.

**Smallest fix:** add `'templates/dapr-resiliency.yaml'` to `FLEET_TEMPLATES` (`Tiltfile:166-190`) so one owner renders both the pods and the policy. (Also latent: the `singleTenant` loop at `dapr-resiliency.yaml:96-102` appends `$name`, the `services` map key, rather than `$svc.daprAppId` — it would mis-target any service whose key diverges from its app-id.)

---

#### M3 · The IIIF bronze page lane has a producer and no consumer

`POST /ingest-iiif` writes `bronze$pages` (`chart/values.yaml:597` `iiifBronzeDataset`); `/bronze-arrival` publishes `medallion.bronze` carrying `dataset: bronze$pages` (`services/medallion/src/medallion/services/ingest_trigger.py:112-121`). The only subscriber on that topic is `bronze-to-silver`, whose `MEDALLION_FROM_DATASET` is `bronze$events` (`chart/values.yaml:629` → `chart/templates/medallion.yaml:276`), and:

```
services/medallion/src/medallion/services/transform.py:109-121
arrived = data.get("dataset")
if arrived is not None and arrived != settings.from_dataset:  # → DROP (ack)
```

Every IIIF ingest is **acked and dropped**. `chart/values.yaml:629-637` declares only `bronze-to-silver`, `silver-to-gold`, `media-to-silver` — the P7b HTR movers CLAUDE.md describes have no entry.

Two precisions: the drop is *not* silent (`transform.py:117-121` logs `medallion_stage_other_lane` with `arrived`/`expects` and increments `record_other_lane`, pinned by `tests/unit/test_medallion_cascade.py::test_the_dropped_lane_is_observable`), and `medallion.bronze` is not a zero-consumer topic — it is the `bronze$pages` **lane** multiplexed onto a shared topic that has no consumer. Pre-guard, the page arrival drove the events mover into a deterministic FAIL/DLQ; today the bronze write lands and nothing downstream runs.

**Smallest fix:** land the P7b page-lane mover as a `medallion.movers` entry (`chart/values.yaml:629`). Until then this is a known, instrumented gap — see `docs/architecture/live-proof-2026-07-28.md` "NOT PROVEN #1".

---

#### M4 · The committed retry-window fix has never been deployed — the live pub/sub retry schedule is ~4s, not 450s

Surfaced while refuting a "everything byte-matches" claim; it is real and live-effective.

`chart/templates/dapr-resiliency.yaml:9-24` documents commit `8060594` (2026-07-28 21:17, *"fix(dapr)!: the retry window was 4 seconds"*), whose only value change was `-duration: 30s` → `+initialInterval: 30s / +multiplier: 2 / +randomizationFactor: 0`. The commit is an ancestor of HEAD and the file is clean. But:

```
helm get manifest rask --revision 17   # today 09:39, ~12h AFTER the fix
  → rask-pubsub-resiliency still has: policy: exponential, duration: 30s
kubectl get resiliency rask-pubsub-resiliency  # created 2026-07-28T11:13:42Z — same pre-fix form
kubectl get crd resiliencies.dapr.io -o json | grep -c '"default"' → 0   # not CRD defaulting
```

Under `policy: exponential`, Dapr ignores `duration`, so `initialInterval` falls back to its 500 ms default: the schedule actually running is 0.5+0.75+1.125+1.7 ≈ **4 s** instead of 30+60+120+240 = **450 s**, ~125× short — and because the sidecar dead-letters and ACKs on exhaustion, the 720 s broker `ackWait` on the `lineage-pubsub-*` components never covers an app-returned RETRY. Confirmed loaded: `rask-lineage-…` daprd logs `Loading Resiliency configuration: rask-pubsub-resiliency` at 09:40:35 today. All other 14 Dapr CRDs byte-match.

**Smallest fix:** `helm rollback rask 15` (revision 15 is the last `deployed`; 16 is `pending-upgrade`, 17 `failed`), then a clean `helm upgrade` / `make k3s-up` from HEAD, then re-diff render vs live. Coordinate with Tilt — it owns the app Deployments.

---

#### M5 · `chart/templates/dapr-statestore.yaml` is missing the `dapr.enabled` gate — `dapr.enabled=false` breaks the whole install

`dapr-statestore.yaml:1` is `{{- if .Values.stateStore.enabled }}` with **no** dapr gate, while every sibling Dapr-CR template has one (`dapr-component.yaml:1`, `compaction.yaml:4`, `services.yaml:182`, `dapr-resiliency.yaml:1`, `observability.yaml:18`, `dapr-inject-sweep.yaml:1`, `dapr-dashboard.yaml:1`), and `compaction.yaml:7-8` states the rationale verbatim.

```
helm template rask chart --set dapr.enabled=false | grep -c 'kind: Component' → 1
  → metadata.name: lance-statestore, apiVersion: dapr.io/v1alpha1
helm template chart --include-crds --set dapr.enabled=false | grep -c components.dapr.io → 0
apply → no matches for kind "Component" in version "dapr.io/v1alpha1"
```

`dapr.enabled=false` is a supported mode (`chart/Chart.yaml:61-64` `condition: dapr.enabled`; six templates gate on it), it is untested (no render with `dapr.enabled=false` anywhere in `.dagger/` or `scripts/`), and `chart/crds/` holds only `cnpg-crds.yaml` — the Component CRD ships solely inside the gated subchart.

**Smallest fix (one line):** `chart/templates/dapr-statestore.yaml:1` → `{{- if and .Values.dapr.enabled .Values.stateStore.enabled }}`. The existing single `{{- end }}` at `:72` already closes it.

---

#### M6 · `lance-statestore` references a secret store that need not exist — `openbao.enabled=false` ships a broken component today

`dapr-statestore.yaml:65` renders `secretStore: {{ .Values.stateStore.secretStore }}` (= `lance-secrets`, `chart/values.yaml:746`) and resolves the connectionString through it (`:53-56`), gated only on `stateStore.enabled` (default true). But `lance-secrets` renders only under `lance.secretsViaDapr` = `openbao.enabled or openbao.externalAddr` (`chart/templates/_helpers.tpl:572-574`, used at `dapr-component.yaml:114`).

```
helm template rask chart --set openbao.enabled=false   # exit 0
  → lance-statestore with `secretStore: lance-secrets` (r2.yaml:33830)
  → ZERO secretstores.hashicorp.vault components
```

That is not hypothetical: `scripts/ray_e2e_stack.sh:102` runs exactly that recipe (also documented at `tests/e2e-py/test_media_e2e.py:68`, `tests/e2e-py/test_governed_union_e2e.py:29`). Adding `--set media.enabled=true` puts the annotator in the blast radius too — `services/annotator/src/annotator/main.py:89-90` `register_actor` fails and every actor proxy call fails after it. `dapr-component.yaml:163-171` documents this exact failure chain ("references a secret store that isn't loaded: lance-secrets" → "Actor state store not configured - actor hosting disabled") but guards only scope drift, never the component's existence. `tests/unit/test_invariants.py:625` asserts a `secretKeyRef` *has* an `auth.secretStore` — never that the named store exists — and renders at defaults, so it cannot catch this.

Correction to the record: `actors_registered` (`main.py:91,94`) is written and **never read** anywhere in the repo; the "503" comment at `main.py:86` is aspirational. The failures surface as unhandled exceptions (500s), not clean 503s. Both defaults are safe (`media.enabled: false`, `openbao.enabled: true`, `values-prod.yaml:123` keeps OpenBao on), so prod is unaffected — this is a latent defect on an opt-in combination that a live e2e script already hits.

**Smallest fix:** a `fail` guard in the chart when `stateStore.enabled` and not `lance.secretsViaDapr` (the chart already has this culture — `auth-consistency.yaml`, `gpu-coherence.yaml`, `age-cluster.yaml`, `ray-auth-token.yaml`), or a plaintext-env fallback under `not secretsViaDapr` like `services.yaml`/`medallion.yaml`/`compaction.yaml` already have.

---

#### M7 · `lance-statestore` can never hot-reload; daprd errors once per minute forever on annotator + catalog

Because `dapr-statestore.yaml:58` sets `actorStateStore: "true"`, Dapr 1.18's hot-reload reconciler refuses updates and errors on every resync tick:

```
level=error msg="Aborting to hot-reload a state store component that is used as an actor
 state store: lance-statestore (state.postgresql/v1)" scope=dapr.runtime.hotreload.reconciler
10:22:36.316  10:23:36.316  10:24:36.316  10:25:36.316  10:26:36.317 …
```

Exactly 60.000 s apart, sub-ms jitter, anchored to each sidecar's own `Starting to watch Component updates` (annotator at `:36.316`, catalog at `:13.78` — per-sidecar, not a shared informer). A 141-minute-old pod carried 142 aborts in 290 total log lines. Nothing is changing (`resourceVersion` 63189, `generation` 1, stable across samples) — the diff can never converge, because the abort means the compstore copy is never replaced. `lance-statestore` is the only Component in the estate carrying a Dapr metadata `secretKeyRef`, which is consistent with the resolved-secret in-place mutation being the permanent diff source. The negative control passes: unscoped `viewer`/`search`/`controlplane` pods at 141 min have 0 aborts.

The flag is **not** removable — `services/annotator/src/annotator/main.py:78` registers the estate's first actors and `projects/actor.py:14` persists reminders there; placement logs `Dissemination complete for version 11 (changed types [AnnotationProjectActor AnnotationTaskActor])`.

Corrected blast radius (the original claim overstated it): this **never reaches GreptimeDB**. `chart/templates/otel-collector.yaml:126-133` `filter/drop_app_file_logs` drops file-tailed records where `resource.attributes["lance.dev/logs"] == "otlp"`, and both scoped pods carry that label. Verified: `opentelemetry_logs` holds 3,884,534 rows, of which `body LIKE '%Aborting to hot-reload%'` = **0**; the negative control (`rask-controlplane`, no label) does land — `body LIKE '%dapr.runtime%'` = 9,021 rows. So the cost is (a) `kubectl logs` noise, 2,880 identical error lines/day across two pods, indistinguishable from a genuine reload rejection, and (b) the real trap:

**Any `helm upgrade` changing `stateStore.tableName`/`metadataTableName`/`componentVersion`/the DSN key updates the Component CR but does not roll the annotator/catalog Deployments — the sidecars keep serving the old config until those pods are restarted.**

**Smallest fix:** add a `checksum/statestore` annotation on the annotator + catalog pod templates so a `stateStore.*` change forces a rollout, and document the constraint next to `dapr-statestore.yaml:58`. The 60 s error itself is upstream Dapr behaviour and not fixable here.

---

### Minors

#### m1 · The medallion producer kept its lance-ns app-id `lance-ray`

`chart/values.yaml:582` `daprAppId: lance-ray` renders `Deployment rask-lance-ray`, `dapr.io/app-id: "lance-ray"`, `OTEL_SERVICE_NAME=lance-ray`, component `lineage-pubsub-lance-ray`, `MEDALLION_DLQ_TOPIC=dlq.lance-ray`, openbao NetworkPolicy client `lance-ray` (`chart/templates/network-policy.yaml:138`), FGA identity `service-lance-ray` (`scripts/seed_medallion_fga.sh`); the gateway names it at `services/gateway/src/gateway/__init__.py:86`. It is the **only** non-bare app-id in the render, and it occupies the slot `docs/architecture/lance-ns-merge.md:250` decision 2 reserves for `medallion`. The one-name-per-surface rule is written verbatim 500 lines above the violation in the same file — `chart/values.yaml:77-79`: *"`compute` on EVERY surface — uv member, import, k8s objects, dapr app-id, image — per R22"*. The producer's uv member and import are `medallion`; everything else is `lance-ray`. No exception was ever recorded (R20 recorded its ray-api PyPI-shadow exception explicitly), and `lance-ray` *is* a third-party PyPI package (`uv.lock:1694`, a `packages/ratch` dep), which makes it worse, not protected. Live in ~20 files (`Tiltfile:264`, `scripts/e2e_stack.sh:128,139`, `tests/unit/test_invariants.py:262`, `tests/e2e-py/*`). The three movers correctly carry bare names — the defect is confined to the producer/ingest head.

**Smallest fix:** rename to `ingest` (R24 already rules the ingest head becomes its own service) or `medallion`, in one sweep across the ~20 sites. Also `rask-lance-ray` breaks naming rule 1 (`lance-ns-merge.md:249`, backends are `rask-<service>`).

#### m2 · `service_kit.build_dapr_client` constructs a **gRPC** client against `DAPR_HTTP_PORT`

`packages/service-kit/src/service_kit/__init__.py:58-63` returns `dapr_client_cls(f"http://127.0.0.1:{settings.dapr_http_port}")` with `config.py:42` `dapr_http_port = "3500"`. daprd's gRPC port is 50001. Proven against the live sidecar:

```
:3500  → DaprGrpcError UNIMPLEMENTED "Received http2 header with status: 404"
:50001 → OK app_id=controlplane
```

Only `invoke_method` would accidentally survive (it reads `DAPR_HTTP_PORT` from env, ignoring the address); state, pub/sub, secrets, bindings and metadata all ride the mis-targeted channel. Nothing consumes it — repo-wide, no `DaprClientDep`/`get_dapr` consumer exists; medallion has its own correct env-derived wiring (`services/medallion/src/medallion/api/dependencies.py:17-22`) and the gateway does its own correct HTTP invoke (`gateway/__init__.py:148-158`). Two corrections to the original framing: it is **live-but-unconsumed**, not dead — `RASK_DAPR_ENABLED=true` in-cluster, so `__init__.py:112` really constructs the client for compute and controlplane every boot (it fails to crash only because gRPC channel creation is lazy); and it was **never** consumed by core-api/orchestrator — `git log -S` shows it was born speculative in `cca464e` (2026-06-23) with the mis-port already present, and `packages/service-kit/tests/test_dapr.py:47` pins the wrong address, locking the defect in.

**Smallest fix:** delete the seam (`build_dapr_client`, `get_dapr`, `DaprClientDep`, the lifespan wrapping at `:112-118`, `config.py:42`, `test_dapr.py`). If it is kept, construct `DaprClient()` with no address so the SDK resolves `DAPR_GRPC_PORT=50001`, and fix the test.

#### m3 · `docs/LINEAGE.md:266` says the Dapr-cron reconcile sweep is off by default; it is on

`chart/values.yaml:147-149` is `reconcile: {enabled: true, bindingName: lineage-reconcile-cron}` (flipped in `a541d35`, 2026-07-28, which touched `docs/LINEAGE.md` zero times), and `helm template rask chart/` emits a `bindings.cron` Component `lineage-reconcile-cron`, `@every 300s`, `scopes: [lineage]`. `values-prod.yaml:44-45` pins it true. **Fix:** update `LINEAGE.md:266` to "ON by default (render-gated on `dapr.enabled` + `dapr.sidecars`)", and note the sibling `services.lineage.outbox` (`chart/values.yaml:162`) was flipped ON in the same decision.

#### m4 · `chart/values.yaml:753-757` fabricates the rationale for the catalog's state-store scope

The comment says the scope *"predates that decision"* and is for *"the publish saga's own state"*. The catalog persists **no** saga state; its sole state-store usage is per-subject user state — `services/catalog/src/catalog/main.py:149-156` builds `UserStateStore` unconditionally against `settings.user_state_store` (default `lance-statestore`, `core/config.py:274`, no env override in the chart) and `api/v1/router.py:27,58` mounts `/v1/user-state/{workflow-graph,saved-views,dock-layout}`, consumed by three zones (`frontend/microfrontends/{media,lakehouse,compute}/src/routes/capi/v1/user-state/[document]/+server.ts`) plus `frontend/packages/api/src/dock-layout.ts`. `tests/unit/test_invariants.py:532` already asserts the opposite of the comment and passes. The comment frames as vestigial a scope that is mandatory, inviting the exact edit — trim it — that would break every signed-in user's saved canvas, views and dock layouts. `services/catalog/src/catalog/api/v1/endpoints/user_state.py`'s own docstring already cites `stateStore.scopes` as load-bearing, so the two files contradict each other in-tree. **Fix:** rewrite the comment to name `/v1/user-state/*`, cross-referencing `tests/unit/test_invariants.py:532`.

#### m5 · Small doc/test residues found while verifying

| Site | Problem | Fix |
|---|---|---|
| `tests/unit/test_invariants.py:237-248` | `_helm_template()` `pytest.skip("helm not available")`; the only CI job running `tests/unit` is `ms-test` → `.dagger/test.go:21` → `.dagger/base()`, which has **no helm**. The chart-render invariants (incl. the state-store name/scope guard recorded as *proven*) silently skip in CI. | install helm in `.dagger/base()`, or hard-fail the skip when `CI` is set |
| `CLAUDE.md:120-123` | "every later upgrade is refused until `helm rollback`" — true only while the **latest** revision is pending. Proven: `helm upgrade --install rask ./chart --dry-run` succeeds to rev 18 with 16 pending / 17 failed. | narrow to "if the LATEST `helm history rask` row is pending" |
| `CLAUDE.md:87-119` | "live_update does NOT work here" + the Tilt/k3s-up shared-release warning — superseded by `5c4af7a`, `fef07b9`, `cdb3657` (2026-07-29), which removed `helm_resource` |  rewrite the STATUS block |
| `Tiltfile:287-288` | "Tilt does not override it" — `Tiltfile:207` does (`--set media.enabled=true`) | delete the stale comment |
| `docs/DEPLOY.md:12` | references `make dashboards`; no such target | point at the port-forward in `chart/templates/NOTES.txt:133` |
| `docs/RESILIENCE.md:98` | claims "CI render-asserts the set" of durable consumers; `.dagger/charts.go` only asserts the 720 s backOff | write the assertion or drop the claim |
| — | nothing asserts `chart/templates/configmap.yaml:20` renders `RASK_DAPR_ENABLED` (unlike `LINEAGE_DAPR_ENABLED`, pinned at `tests/unit/test_lineage_emission_wiring.py:241-242`), so a chart edit could silently drop the gateway to the httpx fallback | add the render assertion |
| `chart/templates/frontends.yaml` | zones are sidecar-less **by design** (they present `dapr-api-token` + `x-lance-service-identity` directly — `frontend/packages/api/src/bff.ts:295`, accepted at `packages/lineage-kit/src/lineage_kit/config.py:29`) but, unlike `chart/templates/runners.yaml:29`, carry no comment saying so | add the one-line rationale comment |

### Info

- **Four app-ids run a zero-component daprd — gateway, viewer, search, *and controlplane*** (the original note said three; `rask-controlplane` is injected at `chart/templates/controlplane.yaml:19` with a hardcoded app-id, appears in no Component's `scopes`, and `grep -rni dapr services/controlplane/src` returns nothing). This is correct: they are invoke-only or invoke targets, and a callee needs a sidecar for `/v1.0/invoke`. `lance-secrets` can never gain viewer/search at any `media.enabled` value because `dapr-component.yaml:150-171` builds `$secretScopes` from catalog/lineage/compaction/medallion/stateStore.scopes only — fine, since `media.yaml` sets no `*_SECRETS_FROM_DAPR` (it uses a plain `secretKeyRef` at `:147-149`). **Recording all four so a future audit does not read `controlplane` as a missing scope.**
- **Control plane healthy** — operator/sentry/injector/placement + 3-replica scheduler all 1/1 on `ghcr.io/dapr/*:1.18.1`; `configurations.dapr.io daprsystem` has `mtls.enabled=true`, `workloadCertTTL=24h`, and all 12 sidecars carry `--enable-mtls` (checked every one). Caveats: `dapr-operator` has `restartCount: 2` with `exitCode 255 / reason Unknown` at 2026-07-29T06:12:24Z and a clean `--previous` log; and the 3 scheduler replicas are the subchart's own etcd-quorum default, **not** an HA posture — `chart/values.yaml:1101` sets `dapr.global.ha.enabled: false`, so operator, sentry, injector and placement are each a single point of failure.
- **Sidecar coverage complete** — 12/12 annotation-bearing pods carry daprd; the injector webhook is `failurePolicy: Fail` with `objectSelector matchLabels{dapr.io/enabled: true}`, so an un-injected pod is rejected rather than silently admitted. Snapshot caveat: Tilt was rolling the fleet during the audit (13 pods observed mid-roll), so any point-in-time census is a snapshot.
- **All 19 (component, app-id) scope pairs load; zero init failures.** 11 Components live = 11 rendered. OpenBao→Postgres chain proven past the log line: `secret/lance/dapr-state-connection-string` → `daprstate` DB holds `state` + `dapr_metadata`, migrations=3, last cleanup 2026-07-29 09:59:25+00. All 5 JetStream streams exist with subjects covering every live subscription; no app binds two topics on one stream under one durable name.
- **Zero `Subscription` CRDs is deliberate and does not hide the wiring** — subscriptions are programmatic via `dapr-ext-fastapi`, and the topic/DLQ map is fully visible to `kubectl` on the Deployment envs plus the five per-subscriber `lineage-pubsub-<app-id>` Components (each declaring `queueGroupName`/`durableName`/`scopes`) and the two Resiliency CRs.
- **The pub/sub plane is idle on this k3s cluster** — all 5 streams `last_seq: 0`, 0 B account storage, all subscribers `push_bound: True`. That is a property of *this* install; the cascade head was proven end-to-end on the kind cluster (`docs/architecture/live-proof-2026-07-28.md`: 6 CloudEvents off `lineage.events.v1` → `/bronze-arrival` → `bronze$pages` v1, 10 rows, run in AGE, plus a DLQ park).

---

## 3. Already tracked in OPEN-WORK.md

| This audit | OPEN-WORK item | Status |
|---|---|---|
| M3 (page lane no consumer) | **RASK-INTEGRATION §2 / lance-ray seam contract** (`OPEN-WORK.md:1121-1126, 1180-1202`) — producer/movers are dummy Ray jobs, must become real Ray Data jobs | partially covers it. §2 pins the *contract* (producer must not publish `medallion.bronze` itself) but never says the page lane has no subscriber. **Add the lane mismatch as an explicit sub-item.** |
| M6 (dangling `lance-secrets`) | **RASK-INTEGRATION §1 stateStore row** (`:1106-1113`) and **§4 Secrets two-tier** (`:1132-1136`) | adjacent only. §1 warns that `scopes` must list every app; it does not warn that `auth.secretStore` can name a store that never renders. **Not covered — file M6.** |
| M7 (statestore restart trap) | **ASSESSMENT §3 gap 5 · OpenBao sealed-on-restart** (`:1273`) | different mechanism, same operational family. **Not covered.** |
| m4 (catalog scope comment) | **RASK-INTEGRATION §1 ⚠️ note** (`:1106-1113`) — *"its `scopes` must list every app that owns operational state (today: catalog, annotator)"* | the note is **correct** and `chart/values.yaml:753-757` contradicts it. Fixing the comment closes the contradiction. |
| M4 (undeployed retry fix) | **E2 · Resilience residuals — DLQ bullet** (`:286-288`) — *"poison-inject → Dapr `deadLetterTopic` parking never driven live"* | related: the live retry window being 4 s rather than 450 s makes any future DLQ drive measure the wrong thing. **Fix M4 before driving E2's DLQ check.** |
| m2 (`build_dapr_client`) | **F1 · collision-free sweep / R19** (`:357-388`, CLOSED) — names `service_kit/{dapr_publish,control_events,lakehouse/outbox}.py` as the real homes | F1's scope was `dapr_publish.py`; the mis-ported client seam next door was never in scope. **Not a re-file — new.** |
| M1/M2 (invoke targets with no pod) | — | **nothing in OPEN-WORK covers the invoke plane at all.** |

### Marked done but demonstrably not

1. **`8060594` — "fix(dapr)!: the retry window was 4 seconds"** — committed, in HEAD, referenced by its own template comment, and **never rendered into any release revision**. `helm get manifest rask --revision 17` (12 h after the commit) still carries `duration: 30s`; the live CRD dates to 2026-07-28T11:13:42Z. See M4.
2. **ASSESSMENT §3 gap 9 — "Dapr control plane left non-HA in prod"**, contradicted by the §7 render check at `OPEN-WORK.md:671` claiming *"Dapr-HA on"*. The render check is wrong: `chart/values.yaml:1101` sets `dapr.global.ha.enabled: false`, and live, operator/sentry/injector are 1 replica and placement is a 1-replica StatefulSet. **Gap 9 is still open; `:671` should be corrected.** (Same for gap 2 vs *"alerting on"* — the two records at `:671` and `:1267`/`:1283` cannot both be right.)
3. **UX evidence §14 guard 3 — "drop `auth.secretStore` from the state store" CLOSED/proven** (`:725-730`). The guard exists and works on a dev box, but `tests/unit/test_invariants.py:237-248` skips when helm is absent and the only CI job running `tests/unit` has no helm. **The guard is proven, not enforced.**
4. **`### C3` appears twice** (`:123` un-struck, `:127` struck CLOSED 2026-07-28) — a reader scanning headings sees it as both open and closed. One-line delete.
5. **Stale-in-the-other-direction (items open that are now done)** — `B1 · No actor type registered` (`:59-70`, marked **blocker/OPEN**) is closed: `services/annotator/src/annotator/main.py:78` registers `AnnotationTaskActor` + `AnnotationProjectActor` (landed `4716a1a`, 2026-07-28), the live sidecar logs `Using 'lance-statestore' as actor state store` / `Registering hosted actors` / `Scheduler stream connected for [… JOB_TARGET_TYPE_ACTOR_REMINDER]`, and `/dapr/config` returns both entities. The **workflow** half of B1 remains open. This cascades: `S6`'s *"blocked on S5"* label (`:2337-2343`) is stale per the fence note at `:2314-2317`; `ASSESSMENT §3 gap 17` (*"3×16Gi PVCs for actors/workflows the stack doesn't use"*, `:1299`) is now half-wrong; `DESIGN-interactive-state` inventory (`:1449-1454`) and `§1.2` (`:1736-1747`) both still say *"no state store, no actors"*.
   ⚠️ `§10` (`:2394-2403`, **blocker**) is unaffected and still gates `S6`: the annotator has no verified subject — *do not key actors on `X-User`*.

---

## 4. Not tracked anywhere

1. **M1** — the gateway's `compute` app-id has no pod on any shipped deploy path; `make k3s-up` advertises the 500ing URL and `make dev-frontends-k3s` hangs on it.
2. **M2** — Tilt and Helm disagree about whether the media plane exists; four invoked app-ids run with no invoke policy.
3. **M4** — the retry-window fix is in the chart and has never reached a release revision (live schedule ~4 s vs the intended 450 s).
4. **M5** — `dapr-statestore.yaml` missing the `dapr.enabled` gate; `dapr.enabled=false` is a declared, untested, broken mode.
5. **M6** — `auth.secretStore: lance-secrets` can dangle; `scripts/ray_e2e_stack.sh:102` runs that recipe today.
6. **M7** — `lance-statestore` can never hot-reload; `stateStore.*` chart changes silently need a pod restart.
7. **m1** — `lance-ray` app-id violates the merge's own naming ruling and the one-name-per-surface rule written in the same values file.
8. **m2** — `service_kit.build_dapr_client` builds a gRPC client against the HTTP port, is consumed by nothing, and its test pins the defect.
9. **m3–m5** — the doc/test residues table above.

---

## 5. Checked and fine — do not re-investigate

- **`annotator` in `stateStore.scopes` / `lance-secrets` with no annotator pod at defaults** — a deliberate forward declaration for the `media.enabled=true` posture that every real deploy path uses (`Tiltfile:207`, `Makefile:476`, both verify scripts). Live: `rask-annotator` 2/2 hosting both actors. Removing it would recreate the 2026-07-26 "secret store isn't loaded → actor hosting disabled" failure recorded at `dapr-component.yaml:169`.
- **Zero `Subscription` CRDs** — deliberate (programmatic via `dapr-ext-fastapi`) and *not* an audit blind spot: the full topic/DLQ map is on the Deployments and the five per-subscriber Components, all visible to `kubectl` and to the Dapr dashboard's RBAC.
- **"No Dapr CRD is gated off by default; all 15 render"** — true as a count, but see M4: 14 of 15 byte-match live, `rask-pubsub-resiliency` does not.
- **"The catalog's state-store name is an unpinned coincidence"** — it is pinned by `tests/unit/test_invariants.py:532`, verified by mutation (renaming the component and dropping the scope each redden with explicit diagnostics). The only residue is the CI helm-skip (m5).
- **"The merge disabled the estate's only actor host"** — chronologically impossible: `c0fde61` landed 2026-07-27, the first actor landed 2026-07-28 in `4716a1a`. `media.enabled: false` is a documented opt-in default; the actor host is running right now.
- **"Every Dapr resource carries lance-ns's identity; release inventory splits"** — the release legitimately carries 10 distinct `app.kubernetes.io/name` values (one per subchart); cohesion rides `app.kubernetes.io/instance: rask`, uniform across all 332 occurrences; the split is documented as deliberate at `chart/templates/_helpers.tpl:23-26` and is what makes the selectors safe.
- **"The Dapr dashboard is unreachable and undocumented"** — port-forward-only by design, documented in five places (`NOTES.txt:133`, `docs/DEPLOY.md:12,41`, `docs/DURABILITY.md:117`, `values-prod.yaml:147-148`, `security-sa.yaml:7,10`), and serving HTTP 200 live.
- **"Dapr is absent from the local dev loop"** — absent only from `make dev-micro` (which deliberately takes the httpx fallback); fully present under `make k3s-up`/`make tilt-up`, where the sidecar-invoke branch is executing right now.
- **"The release is `failed` at rev 17 with 16 `pending-upgrade`, blocking upgrades"** — the strings are right, the consequence is not: `helm upgrade --install --dry-run` proceeds to rev 18. Helm's pending gate reads only the *latest* release; rev 17 is `failed`, which upgrades normally. Rev 17's `context deadline exceeded` was the `--wait` timeout, not an apply failure — all 29 deployments are READY. **Do not run `helm rollback rask 15`** as a fix for this (M4 is a separate, deliberate reason to roll back and re-upgrade).
- **"Two owners have written the release — 20 Deployments are `managed-by=tilt`"** — that is Tilt's own apply-time ownership label, not a Helm conflict. `helm_resource` was deleted in `5c4af7a` (2026-07-29) precisely to end the split; the Tiltfile's only helm call is a client-side `helm template` (`Tiltfile:199`), and Tilt has written zero release revisions.
- **"No event has ever traversed the pub/sub plane"** — true of this k3s install only; the cascade head, delivery, retry and dead-letter parking were all proven on kind (`docs/architecture/live-proof-2026-07-28.md`).
- **"`catalog-control-pubsub` is the only pub/sub component with no resiliency policy"** — `lineage-pubsub` is also absent (publish-only), and the catalog's `/control-events` handler unconditionally ACKs `SUCCESS`, so an inbound retry policy could never fire. Empty `deadLetterTopic` is documented design for a best-effort refresh-hint feed.
- **"catalog/lineage consumers are ephemeral while movers are durable — an inconsistency"** — three deliberate, individually documented designs (`dapr-component.yaml:30-31, 47-62, 85-90`; `docs/RESILIENCE.md` gap #3). Adding `durableName` to `catalog-control` (which has no queue group by design) would be a regression into the documented "consumer name already in use" orphan mode.