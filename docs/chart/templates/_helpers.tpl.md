# chart/templates/_helpers.tpl

Rationale for `chart/templates/_helpers.tpl`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- define "rask.name" -}}`

=====================================================================================================
MERGED HELPERS — rask (base) + lance-ns (grafted 2026-07-27, lance-ns main@083b49a).

Both charts install as ONE release (`helm install rask ./chart`, values `fullnameOverride: rask`), so this
file carries BOTH named-template sets. A Go template `define` is last-one-wins and SILENT, so the merge
rule here is mechanical: rask's helpers keep the `rask.` prefix, lance-ns's keep the `lance.` prefix, and
nothing is defined twice. Verified at merge time — the two sets are disjoint (no lance-ns template was
renamed; every `include "lance.*"` in the grafted templates resolves to the original definition).

NAMING CONTRACT (the one thing that makes the two planes agree):

    include "rask.fullname"  → .Values.fullnameOverride            → "rask"
    include "lance.fullname" → .Release.Name                       → "rask"

They render the SAME string only because the release is named `rask` AND `fullnameOverride: rask`. That is
deliberate (ADR 0108: lance object names become `rask-*`), and `lance.fullname`
is kept on `.Release.Name` rather than delegated to `rask.fullname` because ~40 grafted lance templates
build sibling object names from a RAW `{{ .Release.Name }}-…` (the `-dapr-app-token` and `-frontend-session`
Secrets, the `-pubsub-resiliency` Configuration). Delegating would
move the Deployments but not those, splitting the estate the moment release name ≠ fullnameOverride.
Install under any other release name and the two planes diverge — keep them equal.

LABELS: `rask.labels` emits app.kubernetes.io/name=rask, `lance.labels` emits =lance-ns. Kept distinct on
purpose: rask workloads select on {name,instance,component} (fleet.yaml/frontends.yaml)
while lance Services select on {component,instance} only (services.yaml). Different `name` values are what
guarantee a rask selector can never adopt a lance pod inside the shared release.
=====================================================================================================

---------------------------------------------------------------------------------------------------
rask — the base chart's helpers (fleet: flows, notifications, frontends, the cluster route —
the orchestrator died at P7a; core-api/search-api/volumes-api died in the R6/R20 wave).
---------------------------------------------------------------------------------------------------

## `{{- define "rask.componentLabels" -}}`

Component labels: pass (list . "<component>")

## `{{- define "rask.daprPodLabels" -}}`

The ONE store's root identity is objectStore.accessKey (a value) and the store's `object-store-secret-key`
(never a value), held together in `<release>-object-store-root`: the dev store and the scoped-users hook are its
only readers, since on RustFS the root secret signs every STS session token (ADR 0168).

Dapr sidecar pod annotations (no-op unless dapr.enabled) — the ONE annotation surface for the whole
estate: the rask fleet AND the lance planes (services.yaml catalog/lineage,
maintenance.yaml, explorer.yaml viewer/search) all render
THIS helper, so the sidecar contract can never drift between planes (DAPRIFY 2026-07-27). Carries the full union of what the two planes shipped:
  - enabled / app-id / app-port / log-level (the original rask surface)
  - max-body-size (when dapr.maxBodySize is set — Dapr's 4Mi default rejects multi-image batch uploads)
  - app-token-secret → the shared <release>-dapr-app-token Secret (ESO-written, external-secrets.yaml,
    same dapr.enabled gate, so the reference can never dangle): Dapr injects APP_API_TOKEN and stamps
    `dapr-api-token` on delivered requests → sidecar-only routes reject forged direct POSTs
  - the daprd resource bounds + disable-builtin-k8s-secret-store (+ optional seccomp) via
    lance.daprSidecarResources
  - dapr.io/config: lance-tracing — UNCONDITIONAL. It was gated on lance.otelEnabled, which was
    right while that Configuration held only tracing and wrong the moment it also held the
    workflow state-retention policy: turning telemetry off silently turned retention off with it,
    and workflow history is then kept forever. The object now always renders (the tracing STANZA
    inside it is what carries the otel gate), so this reference cannot dangle — which is the
    property the old gate existed to protect.
Usage: {{- include "rask.daprAnnotations" (list $root $appId $appPort) | nindent 8 }}

---------------------------------------------------------------------------------------------
DAPR SIDECAR INJECTION.

Dapr's sidecar injector is a MUTATING WEBHOOK, and its MutatingWebhookConfiguration ships
`failurePolicy: Ignore` by default: a pod created while the injector is unreachable (a restart, an
outage) is SILENTLY admitted unmutated and comes up with NO daprd container at all. Nothing recreates
that pod — a CrashLoopBackOff restarts the container inside the SAME pod, which will never gain a
sidecar — so the app crash-loops forever on "secret unavailable from Dapr store … failing closed".

Fix, in the control plane rask runs against (a prerequisite; deploy/fixtures/dapr/values.yaml in
development): `failurePolicy: Fail`, scoped by an `objectSelector` to pods carrying this LABEL. Fail-closed
admission makes the API server REJECT the create instead of silently dropping the sidecar; the
ReplicaSet controller retries, and the first retry after the injector is Ready produces a properly
injected pod. The objectSelector is what makes `Fail` safe: an unscoped fail-closed pod webhook
would also block the injector's OWN pod (and every infra pod) and wedge the cluster permanently.

The label must therefore appear on EXACTLY the pods that carry the `dapr.io/enabled` ANNOTATION —
a pod with the annotation but no label is admitted unmutated again (the silent failure, restored),
and one with the label but no annotation just costs an admission round-trip. That correspondence
is mechanical, so it is a render guard:
test_invariants.py::test_every_dapr_annotated_pod_carries_the_injector_webhook_label.

## `dapr.io/log-as-json: "true"`

Hardcoded, deliberately NOT a values knob: the Collector's filelog json_parser is scoped to the
daprd container and is only correct while this is true. A knob would let someone turn the sidecar
plane back into unparsed logfmt with no gate firing.

## `dapr.io/block-shutdown-duration: {{ printf "%ds" (int $root.Values.lifecycle.sidecarBlockShutdownSeconds) |...`

THE SIDECAR MUST OUTLIVE THE APP'S DRAIN, not race it. This block emitted nothing about
shutdown, so daprd took its 5s default while the app was still inside its own preStop sleep —
and since the kubelet SIGTERMs every container simultaneously, the sidecar was gone before the
app had begun draining. A handler that finished its Lance write and then published its event at a
sidecar that had already stopped landed the write and lost the event, which is indistinguishable
from a write that announces nothing.
Derived from the same `lifecycle` block that feeds preStop and the grace period, so the three
numbers cannot drift — see the arithmetic in values.yaml.

## `{{- if $root.Values.dapr.enabled }}`

DANGLING-REFERENCE GUARD. The `lance-tracing` Configuration renders inside
`{{- if .Values.dapr.enabled }}` (observability.yaml:18), while this annotation used to be
emitted on `dapr.enabled` alone. Measured with `dapr.enabled=false`:
14 pods annotated, 0 Configurations rendered — every sidecar referencing an object that does
not exist. observability.yaml's own comment says the guard it removed was "against a DANGLING
dapr.io/config reference"; this is that guard, put back on the side that can see both.

## `{{- if has $appId (splitList "," (include "lance.secretScopes" $root)) }}`

THE PER-APP CONFIGURATION WHEN THIS APP-ID MAY USE THE SECRET STORE ([[XC-072]]), else the
shared one. A sidecar references exactly one `dapr.io/config`, and `spec.secrets.scopes` lives
only on a Configuration -- so an app-id that reads secrets needs its OWN, carrying the same
spec body plus a deny-by-default scope. An app-id with no store access has nothing to scope and
keeps `lance-tracing`, which also keeps the dangling-reference guard above honest: both names
render under this same `dapr.enabled` condition.
DERIVED FROM `lance.secretScopes`, the same list that decides the Component's own `scopes:`, so
a new consumer added there gets its Configuration without a second edit here. The same membership
mounts the pod's store token into daprd (`lance.storeIdentity`): a sidecar the component is scoped
to always has the projected token it logs in with.

## `{{- define "lance.daprOtlpTarget" -}}`

OTLP/OpenTelemetry container env for the rask FLEET (no-op unless observability.enabled). Shared by
fleet so the OTLP wiring never drifts.
Usage: {{- include "rask.otelEnv" (list $root "service-name") | nindent 12 }}

Every plane exports to the in-chart Collector (`lance.otlpEndpoint`), never to a store: the app stays
backend-agnostic and the Collector adds the backend's headers (`observability.export`).

## `{{- define "rask.otelResourceAttrs" -}}`

THE single OTel resource identity for every pod in this chart — fleet, lakehouse and Ray.
Call: {{ include "rask.otelResourceAttrs" $root }}

ONE derivation, because two had drifted. The fleet emitted
`service.namespace=rask,deployment.environment=<Release.Namespace>` while the lakehouse and Ray
emitted `service.namespace=lance-ns,deployment.environment.name=<observability.environment>,
service.version=<chart>` — ZERO key overlap, so any cross-plane filter, join or dashboard variable
silently saw one half of the estate. Measured 2026-08-23: GreptimeDB had already materialised both
as separate physical columns in `opentelemetry_traces`, with 265,978 of 347,924 spans in a 3-hour
window invisible to a query written against the other name.

`deployment.environment` was RENAMED to `deployment.environment.name` in OTel semconv v1.27.0 and
the old key is marked deprecated. Only the new key is emitted — NOT both. A dual-write is a
permanent band-aid with no removal trigger, it breaks nothing here (nothing in this repo queries
the old name), and it would not close the seam anyway: the historical column already exists.

`service.version` is the CHART version, not the running image tag — it answers "which chart
rendered this pod", not "which build emitted this span".

## `{{- define "rask.zoneOtelEnv" -}}`

THE SSR ZONES' OTel env — a FOURTH rendering, and deliberately not a fourth source of truth. Every
value below comes from `lance.otlpEndpoint` / `rask.otelResourceAttrs`,
the same derivations the other three use. What differs is only WHICH variables are emitted.

`rask.otelEnv` cannot simply be reused here, and trying it is what produced this helper. That one
renders RASK_OTEL_ENABLED — `service_kit`'s PYTHON opt-in, read by `Settings.otel_enabled` and
meaningless to a Bun process — plus OTEL_PYTHON_FASTAPI_EXCLUDED_URLS, which names a framework these
pods do not run. Worse than merely inert: RASK_OTEL_ENABLED is one of the two flags that mark a pod
as an OTLP LOG producer, and the zones export TRACES ONLY. A zone carrying it reads as a log
exporter, which would earn it the `lance.dev/logs: otlp` label and delete the stdout of seven pods
that have no other copy.

No metric interval either: the zone SDK builds a tracer provider and no metric reader.

## `{{- if include "lance.otelEnabled" $root -}}`

GATED ON `lance.otelEnabled`, NOT on `observability.enabled`. The narrow gate meant the chart's own
documented prod posture — ship OTLP off-cluster, deploy no in-cluster stack — rendered ZERO OTEL_*
on the entire request-serving fleet while the lakehouse plane kept exporting: a fleet container
came back as literally `env: null`. Nothing announced it: `setup_otel` returns False, every pod
stays Ready, and every trace that crossed the fleet lost the span that should have parented it. The identical defect was found and fixed for RAY alone in August 2026; the
fleet was left behind and pinned by nothing.

## `- name: RASK_OTEL_ENABLED`

The fleet's `setup_otel` is opt-in on this flag, unlike the lakehouse pods which are launched under
`opentelemetry-instrument` and need none. It MUST stay paired with the endpoint: drop it and
setup_otel returns False while every OTEL_* var below still renders.

## `- name: OTEL_EXPORTER_OTLP_ENDPOINT`

One endpoint derivation for the whole chart, the Collector, so every plane gets its k8sattributes
enrichment and none names a store.

## `- name: OTEL_METRIC_EXPORT_INTERVAL`

BOTH OF THESE TAKE EFFECT WITHOUT `opentelemetry-instrument`, which is why the fleet is not exempt
from them — it was just unset. Verified against the installed SDK rather than assumed:
`PeriodicExportingMetricReader.__init__` reads OTEL_METRIC_EXPORT_INTERVAL, and the FastAPI
instrumentor reads OTEL_PYTHON_FASTAPI_EXCLUDED_URLS itself. The vars that genuinely ARE
launcher-only — OTEL_{TRACES,METRICS,LOGS}_EXPORTER and
OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED — are deliberately NOT copied: they live in
`opentelemetry.sdk._configuration`, which only the launcher runs, and the fleet already makes the
equivalent calls explicitly in `setup_otel`.

## `- name: OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`

The probes are hit every few seconds on every pod, so on a quiet estate the health checks ARE the
request rate: the RED dashboard reads busy and the latency distribution is dominated by a route
nobody calls.

## `value: "/livez,/readyz,/healthz,/api/health,/metrics"`

MUST cover every path `fleet.yaml` points a probe at — NOT just the operational pair. A list
that leaves out `/api/health` lets every service taking the default health path trace its own
kubelet polls twice a second: measured 2026-08-23, 31,916 spans in three hours across four such
services, eight times the Dapr workflow keepalive. Each probe costs SEVERAL spans, not one — ASGI
instrumentation emits a child per send/receive, which is why `GET /api/health http send`
outnumbered `GET /api/health` three to one.

## `{{- define "lance.fullname" -}}{{ .Release.Name }}{{- end -}}`

---------------------------------------------------------------------------------------------------
lance-ns — grafted verbatim from lance-ns main@083b49a chart/templates/_helpers.tpl.

Every one of these is referenced by a grafted lance template (services.yaml, maintenance.yaml,
explorer.yaml, fleet.yaml, object-store-scoped-users.yaml, openbao-seed.yaml, age-postgres.yaml,
otel-collector.yaml, network-policy.yaml, ha.yaml, …). None of them
was renamed: no lance name collided with a rask name.

VALUES THEY REQUIRE (the values.yaml merge must land these or the render breaks):
  age.externalHost · auth.enabled · catalog.controlEmit · dapr.{enabled,sidecarRestricted,
  sidecarResources.*,resiliency.enabled} · auth.oidc.{issuer,clientId} · dex.{enabled,port} · frontend.{apps,image.tag,
  idleTimeoutSeconds,oidc.*} · route.edgeUrl · image.catalog.{repository,tag} · lifecycle.preStopSeconds ·
  medallion.buckets · nats.url ·
  observability.{enabled,environment} · openbao.{enabled,port,externalAddr} · pubsub.name ·
  resources.{default,<component>} · objectStore.{bucket,port,externalEndpoint} · security.readOnlyRootFilesystem ·
  services.{catalog,lineage}.{port,daprAppId} · services.lineage.relay.bindingName
`objectStore.bucket` (singular) is the one that is easy to lose: rask's values ship `objectStore.buckets` (a
LIST) and lance's templates read `objectStore.bucket` (a STRING) — both must exist.
---------------------------------------------------------------------------------------------------

Release name is the fullname (install as `helm install rask ./chart` → all names = rask-*).

## `{{- define "rask.bootstrapRev" -}}r{{ .Release.Revision }}{{- end -}}`

---------------------------------------------------------------------------------------------
BOOTSTRAP JOBS — the `helm install --wait` deadlock fix (live-proof 2026-07-28, defect 1).

A Job that another release resource must wait for IN ORDER TO BECOME READY cannot be a
post-install hook. helm's order is: pre-install hooks → apply manifests → (--wait) block until
every resource is Ready → post-install hooks. So `--wait` blocks on OpenFGA, OpenFGA cannot
start against an unmigrated schema, and the migration is queued BEHIND the wait. Circular; revs
1 and 2 died on "context deadline exceeded" and the OpenBao seed never fired, which is the only
reason scripts/e2e_stack.sh exists (it drops --wait and re-sequences by hand).

Moving the hook EARLIER does not fix it: a `pre-install` hook runs before ANY release manifest
exists, so the migrate Job would wait for an AGE Postgres that has not been created yet — the
same deadlock, one phase further left, and now it also blocks every OTHER resource.

The fix is to take those four Jobs OUT of the hook lifecycle entirely and apply them in the same
wave as everything else. `--wait` then waits for the Job to Complete *concurrently* with waiting
for the servers to become Ready, and the dependency resolves itself: the migrate pod's wait-age
init loops until the AGE StatefulSet (applied in the same wave) answers, migrates, exits; OpenFGA
stops crash-looping and goes Ready; `--wait` returns. No wrapper script, no ordering knowledge
outside the chart.

Jobs that depend on the APPS instead (bootstrap-admin needs a booted catalog) stay post-install hooks — nothing waits on them, which is precisely
what a post-install hook is for.

A plain Job's spec is IMMUTABLE, so the name carries the release revision: an upgrade renders a
new name (new Job, re-runs — the same semantics `before-hook-creation` gave the hooks), helm
deletes the previous revision's Job because it left the manifest, and a re-render of the same
revision is byte-identical (no patch, no immutability error).
---------------------------------------------------------------------------------------------

## `{{- define "rask.isRealDeployment" -}}`

IS THIS A REAL DEPLOYMENT? One definition, because two are two chances to answer it too narrowly at
once — the argument `prod-credentials.yaml` makes for itself and which now has a second consumer.

BOTH SIGNALS ARE FACTS THE DEPLOY ALREADY DEPENDS ON, never a flag someone must remember to arm. A
guard reachable only by remembering an opt-in protects the installs that did not need protecting.
`openbao.enabled=false` (the platform's secret store) is the explicit prod signal; failing that, images pulled from a registry off
this node means the deployment is not the local loop — `make k3s-up` renders `localImages=true` for
side-loaded images, and the three dev-registry addresses are unroutable from anywhere but the node.

Answers the string "true" or "" so a caller can use it in a `ternary` or an `if`.

## `{{- define "lance.ageHost" -}}{{ .Release.Name }}-age{{- end -}}`

In-cluster hostnames (subcharts derive their service name from the release).

## `{{- define "lance.objectStoreHost" -}}{{ include "rask.fullname" . }}-rustfs{{- end -}}`

The ONE object store's S3 host — the Service in front of the rustfs fixture's StatefulSet ([[XC-131]]).
P4's store unification (docs/adr/0124-chart-unification-one-control-plane-each-one-object-store.md) still holds ("the ONE store"); what changed with LH-133 is the store and
therefore its Service name, since a first-party StatefulSet names its own Service
to name is deleted, so fleet + lakehouse + observability all resolve the same endpoint.

## `{{- define "lance.catalogImage" -}}`

The catalog-family image (catalog, lineage, maintenance, explorer, the bootstrap
job) — every one of them runs the SAME image with a different entrypoint.

It DELEGATES to `rask.image`, which is the estate's one image contract: registry prefix, digest
pinning, and the side-loaded (`image.localImages`) form. This helper used to render
`printf "%s:%s" repository tag` on its own, which honoured NONE of them — so eleven containers came
out as the bare `lance-rest-catalog:dev`, i.e. Docker Hub, on a chart that had been configured with a
registry and a digest. That is not a cosmetic drift: it is why every deploy needed `kubectl set image`
fix-ups afterwards, and why a GitOps reconciler could never own this release.

`image.catalog.repository` survives as an OPTIONAL per-component name override (the component is not
named after the chart), and the tag/digest/prefix rules are now whatever the rest of the estate uses.

## `{{- define "lance.s3Endpoint" -}}`

CONSUMER endpoints — return the EXTERNAL override when set (the in-cluster component is then usually
disabled, e.g. a managed S3 / Postgres / Vault / collector in prod), else the in-cluster address. The
component's OWN Service/StatefulSet keeps the plain *Host helper above; only the apps that CONNECT switch.
This is what makes the docs/DURABILITY.md tier-3 externalization real (values-prod.yaml sets the overrides).

THE S3 ENDPOINT HAS NO SILENT DEFAULT: with `objectStore.enabled=false` and no `externalEndpoint` the render fails.
The in-cluster address names the dev store's Service, so an estate without that store would otherwise dial a host
nothing serves; the S3 behind a real estate is the platform's, and the chart only names it.

## `{{- define "lance.otlpEndpoint" -}}`

The OTLP endpoint every plane exports to: the in-chart Collector, rask's one telemetry seam (ADR 0153). Where the
telemetry goes after it is `observability.export`, the Collector's business alone.

## `{{- define "lance.otelEnabled" -}}`

Whether the apps carry the OTel SDK wiring (instrument + otelEnv + the lance-tracing Dapr config): exactly when
`observability.enabled` deploys the Collector they export to. Non-empty string = on (helm `if` truthiness).

## `{{- define "lance.platformStoreAddr" -}}`

THE PLATFORM'S STORE IS NAMED, AND OVER TLS ([[XC-149]]). The chart renders no secret store, so with
`openbao.enabled` off the address is required: a render that names no store would leave every
`secretKeyRef` resolving against Kubernetes Secrets the chart never declares (dapr-statestore.yaml).
An http address is refused because every sidecar presents the store's token to it, in the clear
otherwise; the dev store alone is http, inside the namespace.

## `{{- define "lance.vaultAddrFQDN" -}}`

The SAME address, FULLY QUALIFIED — for a caller that is not in this namespace.
A bare service name resolves only within its own namespace. Every Dapr sidecar consuming
`lance.vaultAddr` sits beside the workload in this release's namespace, so the short form is
correct there. The ExternalSecrets OPERATOR does not: it runs in `external-secrets`, and its
SecretStore login failed with `lookup rask-openbao ... server misbehaving` — a DNS failure that
reads as an OpenBao outage rather than as a name that cannot resolve from where it is used.
Measured against the live cluster 2026-08-31.

## `{{- define "lance.busEnv" -}}`

The bus a pod's sidecar connects to, named to the pods whose sidecar holds a `pubsub.jetstream` component (catalog,
lineage, maintenance, notifications). Each service watches its own sidecar with it and restarts the sidecar once one of
its connections has closed for good and the bus greets a new connection again (`service_kit.bus_watchdog`, [[XC-095]]):
Dapr 1.18.1's component never reconnects past nats.go's default 60 attempts (components-contrib#4563). An address, not
a credential: the sidecar's own connection carries the app's bus user.

## `{{- define "lance.subPubsub" -}}`

The per-SUBSCRIBER pubsub component name (call: include "lance.subPubsub" (list $root <appId>)).
Each subscriber app-id gets its OWN pubsub.jetstream component carrying its queueGroupName — one shared
component cannot: lineage AND notifications both consume lineage.events.v1, so a single queue group would
SPLIT those messages across the two apps instead of duplicating per app / competing per replica. The
component (dapr-component.yaml) and the app's *_PUBSUB env must agree on this name — hence one helper.

## `{{- define "lance.natsTable" -}}`

The NATS permission table as issued (values.yaml `nats.auth`): `users`, plus each `flagged` grant whose value path is true.

## `{{- define "lance.natsUsers" -}}`

The NATS users of lance.natsTable, sorted and validated: a name becomes a secret name and a shell word, a subject a single-quoted shell word.

## `{{- define "lance.natsCreds" -}}`

A pub/sub Component's credential (list $root $appId): its one app's user, from the store (values.yaml `nats.auth`).

## `{{- define "lance.natsAuth" -}}`

The store that resolves a Component's credential, only while one exists (a null `auth:` is refused by the CRD).

## `{{- define "lance.publishPubsub" -}}`

A PUBLISH-ONLY pub/sub Component (list $root $name $appId): one app's connection, carrying no consumer settings.

## `{{- define "lance.mintsDevCredentials" -}}`

Whether the chart generates the dev OpenBao's credentials (external-secrets.yaml): only for the dev store (`openbao.enabled`), which the openbao fixture runs and this chart seeds.

## `{{- define "lance.natsMints" -}}`

Whether the dev OpenBao issues the NATS credentials: the dev store this chart seeds is the issuer of the dev bus (deploy/fixtures/nats).

## `{{- define "lance.natsFiles" -}}`

The Secrets ESO writes for the NATS pods that have no sidecar, as JSON {target: [store secrets]}. This chart's ExternalSecrets write `nats-admin-creds`; the ESO policy grants all of them, because the NATS fixture's ExternalSecrets read the other two (nats-box's `monitor` creds, the server's operator config and route credential) from the dev store.

## `{{- define "lance.daprSidecarResources" -}}`

daprd sidecar resource annotations — the app containers are bounded (resources.default), so the
sidecars must be too (an unbounded daprd per pod × 8 pods can starve a small node). One helper = one
place to size them.

## `dapr.io/disable-builtin-k8s-secret-store: "true"`

daprd AUTO-REGISTERS a built-in `kubernetes` secret store in k8s mode and initialises it at boot,
which builds a client from the pod's k8s-API SA token. We never use that store (our only secret store is
`lance-secrets` — secretstores.hashicorp.vault, scoped per app), but its init is FATAL on failure: the
per-workload SAs set automountServiceAccountToken=false (security-sa.yaml), so the token
file is gone, daprd falls back to `stat /home/nonroot/.kube/config`, and EVERY Dapr-injected pod
CrashLoops ("[INIT_COMPONENT_FAILURE] ... secretstores.kubernetes/v1" — live 2026-07-13; the SA flip was
unshippable). Disabling the unused store is the fix that KEEPS the audit's intent (no mounted JWT, zero
API grants) instead of walking it back by re-mounting the token. Always on: the store is unused with the
flag off too, so this also drops an unnecessary k8s-API surface from the default deployment.

## `dapr.io/sidecar-seccomp-profile-type: RuntimeDefault`

PodSecurity `restricted` compliance for the INJECTED daprd sidecar (our app containers are already
compliant via lance.securityContext; the sidecar is not by default). RuntimeDefault seccomp is the per-pod
annotation; drop-ALL-caps is the injector-wide `dapr_sidecar_injector.sidecarDropALLCapabilities` setting of
the control plane, a prerequisite this chart cannot reach (set it TOGETHER with this flag). OFF by default like
networkPolicy.enabled: full `restricted` ENFORCE is a prod posture, and on this stack it is additionally
BLOCKED by the OTel Collector — a single Deployment whose filelog receiver inherently needs hostPath
(/var/log/pods), which `restricted` forbids and no value fixes. The Collector needs its own namespace at
`baseline`, or a ServiceAccount
PSA exemption in the API-server admission config. So: this hardens what the chart owns; full-namespace
enforce stays parked-by-design (docs/KIND-RUNBOOK §6.4). Live-provable in isolation: flip this + the
injector value, re-roll, and the daprd container carries drop:[ALL] + RuntimeDefault.

## `{{- define "lance.otelEnv" -}}`

OTel SDK env for an app (call: include "lance.otelEnv" (list $root "<service.name>")). The apps run
under `opentelemetry-instrument` and export all three signals OTLP. OTel-first: the target is the Collector
(lance.otlpEndpoint), which adds the backend's headers — so the app stays backend-agnostic (plain OTLP, no
vendor headers). The SDK appends /v1/{traces,metrics,logs} to the endpoint.

NOT the same as "rask.otelEnv": this one is Python-launcher-specific (the OTEL_PYTHON_* knobs).
Both names coexist and do not differ on routing: "rask.otelEnv" derives its endpoint from
"lance.otlpEndpoint" too, so both export to the Collector.

## `- { name: OTEL_LOGS_EXPORTER, value: "otlp" }`

Logs go out via the OTel SDK (OTLP → the Collector → GreptimeDB `opentelemetry_logs`) — the "three
signals, one SDK, all OTLP" path. NO double-ingest: the app pods carry `lance.dev/logs=otlp`, and the
Collector's filelog receiver drops that label (a filter processor), so the file-tailed infra logs (no OTel
SDK) land in the SAME `opentelemetry_logs` table without duplicating the app logs. The filter drops ONLY
what the SDK also exported: a file-tailed line is a duplicate iff a root-logger handler emitted it, and those
all begin with an ISO date — so the condition additionally requires `IsMatch(body, "^[0-9]{4}-...")` and
excludes the `daprd` container. A pre-SDK crash, a uvicorn.error record and the sidecar's own logs therefore
survive in `opentelemetry_logs`, unparsed but present.

DO NOT "SIMPLIFY" THAT BACK TO THE LABEL. It is POD-SPEC metadata, stamped by the ReplicaSet at creation, so
it is never absent during a crash loop. This comment used to claim the opposite, and on that false premise the
filter deleted every crash log and every daprd line on 10 pods — measured 2026-08-23, 0 survivors of 10.

## `- { name: OTEL_METRIC_EXPORT_INTERVAL, value: "5000" }`

Default metric export interval is 60s — too slow to observe in a demo/test. Push every 5s.

## `- { name: OTEL_PYTHON_FASTAPI_EXCLUDED_URLS, value: "/livez,/readyz,/healthz,/api/health,/metrics" }`

Don't trace the k8s probe endpoints — they're hit every 10s and bury real request spans (otel
signals.md § Exclude noisy endpoints). The FastAPI instrumentor reads this itself, launcher or not —
which is why the fleet carries it too.

Must MATCH `rask.otelEnv`'s list byte for byte, so a change to one plane changes the other
in the same commit. Covers every path fleet.yaml points a
probe at, not just the operational pair — including `/api/health`, which remains served as a badge
even though no probe asks for it. `/healthz` matches no route any service mounts.

## `{{- define "lance.lineageEmitEnv" -}}`

lineage-kit's HTTP transport (call: include "lance.lineageEmitEnv" $root).
TWO INDEPENDENT lineage paths exist and they are easy to confuse:

  1. Dapr pub/sub — what maintenance and the catalog use TODAY
     (<APP>_LINEAGE_TOPIC → NATS → the lineage service's subscriber). Wired separately.
  2. lineage-kit's HTTP transport — a producer builds its emitter with `build_emitter()`, which reads
     RASK_LINEAGE_ENDPOINT.

Path 2's failure mode is silent by construction: with no endpoint, `build_emitter` logs ONE
`log.warning("lineage_http_without_endpoint …")` at startup and returns `NoopEmitter`, which then
drops every event at DEBUG. A pipeline emitting into a no-op is indistinguishable, from outside,
from a pipeline that never emitted — the graph is simply empty and everything reports success.

So the transport is rendered BEFORE anything emits through it: the first real run must not be the
moment we discover the wire was never connected. Points at the lineage service's HTTP ingest
(`POST /api/v1/lineage` — services/lineage/api/v1/endpoints/ingest.py), which is the same graph the
Dapr subscriber writes to, so both paths converge.

endpoint_path is rendered explicitly rather than left to its in-code default: a drift between the two
would 404, and ClientEmitter catches-and-logs transport errors, so that too would fail silently.

## `{{- end -}}`

No credential here: a pod that calls lineage over HTTP authenticates with its projected
`rask-lineage` token (lance.identityTokens), whose subject is its ServiceAccount's (lance.saSubject).

## `{{- define "lance.preStop" -}}`

Rollout drain: a preStop sleep holds SIGTERM until endpoint removal has propagated to kube-proxy /
the Dapr sidecar, so in-flight requests drain instead of hitting connection-refused — this is what makes
the apps' /readyz shutting_down branch actually reachable during a rollout. Pairs with pod-level
terminationGracePeriodSeconds (grace > preStop + app drain). sh exists in every app image
(python-slim for the fleet, oven/bun's debian base for the zones).

## `{{- define "rask.shutdownArg" -}}`

values.yaml lifecycle. Arg: $root, or (list $root <pod grace>)

## `{{- define "lance.resources" -}}`

HTTP health probes for the FastAPI app workloads (catalog/lineage/maintenance). Two
distinct signals: readiness (/readyz) is dependency-aware (503 until the pool/namespace is up AND again once
draining) so k8s only routes traffic to a truly-ready pod; liveness (/livez) is process-up only (never
checks a backend — a slow dependency must NOT trigger a restart loop). Liveness runs slower + more tolerant
(failureThreshold 3 × 20s) so a busy-but-alive worker is never SIGKILLed. One helper = every app agrees.

Soft anti-affinity: spread a component's replicas across NODES so a single node drain/failure can't
take the whole service down — otherwise the prod replicas:2 can co-locate and the PodDisruptionBudget buys
nothing (audit: "the HA replica count buys nothing"). ScheduleAnyway (NOT DoNotSchedule) so single-node
kind still schedules every replica. Gated by the caller on podDisruptionBudget.enabled (the prod HA signal
that also bumps replicas). Call: include "lance.spreadConstraints" "<component-label>". (prod-readiness P2)

Per-workload resource tier: resources.<comp> if defined, else resources.default. Lets a stateful store
(age) or the Arrow-IPC-buffering catalog be sized ABOVE the stateless-pod default without a
per-template edit — just set resources.<comp> in values(-prod). Every workload shared one 1-CPU/512Mi
default before, so the stores + the 256MiB-body catalog were sized like request pods (audit). Call:
include "lance.resources" (dict "root" $ "comp" "catalog"). (prod-readiness P2)

## `{{- define "rask.proxyHeaderArgs" -}}`

THE FLEET'S PROBE TRIPLE — one definition for every fleet.yaml workload.
Call: include "rask.fleetProbes" $svc   (pass an empty dict to take every default).

A hand-written template rendering its own copy of the three probes missed two fixes in a row: the
measured `timeoutSeconds` fix had to be copied across and the startupProbe never arrived at all.
One definition is what stops the next divergence.

THE STARTUP PROBE IS THE POINT. `lance.appProbes` has carried one since prod-readiness P1 and
states the hazard: uvicorn runs the FastAPI lifespan BEFORE it binds the socket, so every
pre-bind second is a connection REFUSED rather than a slow 200 — liveness alone gives a hard
`initialDelaySeconds 15 + 3 x 20s` ~= 55s ceiling and then SIGKILLs into CrashLoopBackOff,
exactly when a dependency is already slow. The fleet's lifespans are not faster than the
lakehouse plane's: notifications does an OIDC discovery fetch, two OpenFGA provision round-trips
(at first-install time, while OpenFGA is itself starting), actor registration and a 10s proxy
warm-up. 30 x 10s = the same 300s budget
the lakehouse plane gets, deliberately not a second number to keep in step.

`timeoutSeconds: 5` on ALL THREE. k8s defaults it to 1, measured racing the OOM killer on compute
(#136): under memory pressure the handler missed the deadline, so kills presented as
CrashLoopBackOff probe failures instead of the OOM they were. Boot is the slowest window a pod
has, so the startup probe is the last place to keep the default.

THE TRUSTED-PROXY POSTURE the fleet images document, rendered so production actually runs it.
Call: include "rask.proxyHeaderArgs" $root   (emits two uvicorn args).

Six dockerfiles end with `CMD ["uvicorn", …, "--proxy-headers", "--forwarded-allow-ips", …]` and
a comment reading as a deployment requirement ("MUST be the cluster edge's CIDR at deploy time
… never '*'"). The chart overrides `command`/`args` for every one of them, so NONE of it ran —
six images documenting a posture production never had. Owner decision 2026-08-27: the chart
carries it.

`127.0.0.1` — uvicorn's own default, and what those CMDs hardcode — is the value you would use
if the proxy were a sidecar in the same pod. It is the Gateway's data plane, in another pod on
another IP, so the match can never succeed and every forwarded header is silently ignored. Set
`forwardedAllowIps` to the data plane's pod CIDR to make the client IP real (`deploy/values-local.yaml`
names k3s's pod CIDR, 10.42.0.0/16, where Traefik runs). The default
stays 127.0.0.1 so this changes nothing silently on an existing install.

`*` IS REFUSED HERE, not in a comment. It trusts every peer, so any client's `x-forwarded-for`
becomes the resolved client IP — the dockerfiles already said "never '*'", and this finding is
precisely about a comment not being a mechanism.

## `startupProbe:`

startupProbe gates liveness+readiness until boot completes: the FastAPI lifespan (Dapr secret fetch
~80s worst case + AGE pool + DDL + FGA provision) runs BEFORE uvicorn accepts connections, so nothing
answers /livez during boot. Without this, liveness (armed ~70s in) SIGKILLs a still-initializing pod
into CrashLoopBackOff exactly when a dependency is already slow. 30×10s = 300s boot budget, then the
fast liveness/readiness cadence takes over. (prod-readiness P1)

## `{{- define "lance.tcpProbes" -}}`

TCP health probes for non-HTTP-health workloads — the SvelteKit web pod (no /readyz route) and the object store
(S3 API, no health route). A successful TCP accept on the serving port is the liveness/readiness signal.
Call: include "lance.tcpProbes" "<portName>" (the named container port to dial).

## `{{- define "lance.rootSecurityContext" -}}`

Container hardening applied to every APP container (our images: catalog/lineage/web/maintenance).
runAsNonRoot enforces the image's non-root USER (catalog uid 10001, web `bun`) at admission — a manifest that
regressed to root fails to start instead of running privileged. drop ALL caps + no privilege escalation +
the RuntimeDefault seccomp profile = the restricted PodSecurity baseline. readOnlyRootFilesystem is on by
default (values.security.readOnlyRootFilesystem) — each app mounts an emptyDir at /tmp for scratch (pyarrow
spill, OTel), so nothing needs a writable rootfs. Container-level (NOT pod-level) so it never touches the
injected daprd sidecar or the busybox wait-age initContainer (which legitimately runs as root).

The same baseline for a container whose IMAGE runs as root, so the uid must be pinned rather than
inherited. `lance.securityContext` relies on the image's own USER (catalog uid 10001, web `bun`), and
`runAsNonRoot: true` without a uid fails admission on an image that declares none — which is why the
busybox waiters carried a hand-written context instead of the helper.

UNCONDITIONAL, and that is the change ([[XC-061]]). These contexts sat behind
`security.infraContexts.enabled`, which values.yaml defaults OFF — so the pods the flag exists to make
PSA-restricted-compliant were not compliant by default, and the flag's own comment names the
consequence: "labeling the namespace restricted would REJECT this pod until the init is hardened".
Gating the hardening behind a flag postpones exactly the work that makes the flag safe to flip.

`readOnlyRootFilesystem` IS INCLUDED, which the hand-written contexts omitted: both waiters run
`sh -c "until nc -z host port; do sleep 3; done"` and write nothing at all.

Usage: {{- include "lance.rootlessSecurityContext" 65532 | nindent 10 }}

The baseline MINUS the non-root pair, for an image whose tool does not work as any other uid.
MEASURED 2026-09-23 before this existed, because "it needs root" is the easiest claim to accept without
checking: the `nats` CLI answers `could not load schema` for every non-root uid tried — 65532, 1000 and
65534 — on nats-box 0.14.5 AND 0.19.7, while the same command from the same image as root succeeds.
Bisected key by key: `readOnlyRootFilesystem` alone is fine; `runAsNonRoot`/`runAsUser` is what breaks
it. So this drops exactly the pair that fails and keeps the four that do not.

NOT A GENERAL ESCAPE HATCH. A container using this is still named in the hardening ratchet for the two
keys it lacks, so it cannot quietly stay here once the image can run rootless.

Usage: {{- include "lance.rootSecurityContext" . | nindent 10 }}

## `readOnlyRootFilesystem: {{ .Values.security.readOnlyRootFilesystem }}`

UNCONDITIONAL. This used to be relaxed to false whenever `dev.reload` was set, which existed
solely so Tilt's live_update could write into a running container. Tilt is gone (2026-08-04),
and with it the only reason this chart could ever be told to unlock a container's filesystem.
A chart a reconciler applies should not carry a values flag that weakens it.

## `{{- define "lance.tmpMount" -}}`

The writable-/tmp scratch pair that makes readOnlyRootFilesystem feasible: an emptyDir volume + its
mount. Emitted as a container volumeMount via "lance.tmpMount" and a pod volume via "lance.tmpVolume" so a
read-only rootfs still has a place for pyarrow/Lance spill + OTel. Only needed when readOnlyRootFilesystem
is on; harmless (an unused tmpfs) when off, so unconditionally included keeps the templates uniform.

IT CARRIES A sizeLimit, because an emptyDir without one may grow until the NODE's disk is full — and
DiskPressure evicts every pod on that node, not the one that filled it. This volume is also where
starlette spools each multipart file part, so on the media apps it is directly reachable from an
unauthenticated request. With the bound, the kubelet evicts the offending POD and the node survives.
See security.tmpSizeLimit.

## `{{- define "rask.image" -}}`

A FULLY-RESOLVED image reference for a first-party component. Call:
  include "rask.image" (list $root "notifications")

GITOPS IS THE FIRST-CLASS CONSUMER of this chart, and that decides the two rules below.

1. `image.repository` is REQUIRED. An empty one renders a BARE `notifications:dev` — and a
   bare name is not a local image, it is `docker.io/library/notifications:dev`. That only
   appears to work because `make k3s-import` had side-loaded the tag into the node's containerd,
   so the kubelet never had to pull. A reconciler has no such side channel: every pod
   ImagePullBackOffs, and the error blames Docker Hub rather than the missing setting. Measured
   here 2026-08-04, on `controlplane:dev` and `compute:dev`, after a `helm upgrade` reset every
   Deployment to that default.

   Side-loaded images are still supported — but as an EXPLICIT opt-in (`image.localImages: true`),
   never as the fallback you reach by forgetting.

2. `image.digest` wins over `image.tag` when set. A tag is a mutable pointer; GitOps wants the
   deployed artifact to be exactly what the commit says, and a digest is the only reference that
   cannot drift under it. Setting both is not an error — the digest simply wins, so an automation
   can keep writing a human-readable tag alongside it.

## `{{- $override := "" -}}{{- if gt (len .) 2 -}}{{- $override = index . 2 -}}{{- end -}}`

Optional 3rd element: an explicit tag that beats image.tag — the per-zone `tag` on a
frontend.apps entry, which is the only thing a zone boundary actually buys (independent
deploy). A digest still wins over it, so a pinned reconciler is never overridden by a tag.

## `{{- $perDigest := "" -}}{{- if $i.digests -}}{{- $perDigest = index $i.digests $name | default "" -}}{{- en...`

PER-COMPONENT pins (#135). The fleet is NOT built as one tag and never has been: on a live
estate this chart's single `image.tag` had to describe four different references at once
(a catalog tag across 11 services, a different backend tag, a zone tag, and an ingest
DIGEST under a repo name the chart cannot even produce). It cannot, so `helm upgrade`
rewrote every image to one tag that was not on the node — measured twice on 2026-08-06,
once taking the whole fleet to ImagePullBackOff and costing 22 `kubectl set image` calls to
recover. That made DEPLOYING A DESTRUCTIVE ACT, which is why an entire day of fixes reached
the cluster by hand instead of through the chart.
`image.digests.<component>` and `image.tags.<component>` are read HERE, so a pin is real
rather than documentation. Precedence, most specific first:
  digests.<c>  >  digest  >  tags.<c>  >  <call-site override>  >  tag
A digest still beats every tag, so a reconciler's content pin is never undone by a tag.

## `{{- printf "%s:%s" $name (required "image.tag must be set" ($override | default $i.tag)) -}}`

Side-loaded: a bare name the kubelet must already hold. Never valid for a remote cluster.

## `{{- define "lance.governedAuthAck" -}}`

── the governed-auth env, defined ONCE ─────────────────────────────────────────────────────
`(include "lance.governedOidcEnv" $root)` -> RASK_OIDC_*, everywhere, for every service.

The helper takes no variable PREFIX: the auth field-set is declared once, in
`service_kit.governed.settings`, and binds one RASK_* name each. A prefix parameter here would
emit variables nothing reads.

Split into OIDC / FGA / PINS rather than one block, because a caller may need a subset: a
service that runs OIDC without FGA would otherwise have an authorizer silently enabled that its
block never asked for.

Safe to set estate-wide: these change behaviour ONLY where a route declares an auth dependency,
so a service with no gated route is unaffected and cannot quietly 401 a surface nobody gated.
(explorer.yaml's reasoning, from #90, where scoping the block to one service left the one that
streams page IMAGE BYTES wide open.)

The acknowledgement a service needs when authentication is deliberately OFF.

`assert_authentication_configured` refuses to boot a governed service whose auth is off with nobody
having said so (Q17-6 / §F2-2), because "off because I meant it" and "off because nothing set it" are
indistinguishable and only the second is a vulnerability. `auth.enabled: false` is a SUPPORTED
toggle — the open dev/demo profile — so the chart says which one it means rather than leaving the
pod to refuse.

Emitted in the `else` of every `if .Values.auth.enabled` that includes `governedOidcEnv`, so the two
can never drift into a profile that sets neither.

## `{{- define "lance.identityTokens" -}}`

A caller's projected identity, one volume per door audience it calls ([[LH-220]], D1); rationale in
values.yaml `auth.serviceAccountIssuer`. Call: include "lance.identityTokens" (list "volumes"|"mounts" <aud>...).

## `{{- define "lance.fleetAudiences" -}}`

A fleet service's door audiences, its `identityAudiences` value: one projected token per door it calls.

## `{{- define "lance.saSubject" -}}`

The subject one of the release's own services is: `kubernetes~system:serviceaccount:<release namespace>:<release>-sa-<name>`
(ADR 0156 C5), the subject of the ServiceAccount its pod runs as. Every grant, author and signing identity the chart
names for a service is this, so none can name a subject the pod does not authenticate as.
Call: include "lance.saSubject" (list $root <name>).

## `{{- define "lance.saAccount" -}}`

The ServiceAccount username one of the release's services runs as, `system:serviceaccount:<release namespace>:<release>-sa-<name>`:
the `sub` the cluster's issuer puts in its tokens, and `lance.saSubject` without the issuer prefix. The object store's
identity provider maps a token to the canned policy its `sub` names, so the scoped-users hook writes the catalog's
policy under this name (ADR 0170).
Call: include "lance.saAccount" (list $root <name>).

## `{{- define "lance.saVerifier" -}}`

The SA verifier a door builds beside Dex: its env, and the API-audience token + cluster CA it fetches
the issuer's keys with. Any token the issuer signs for the door's audience is a subject, from any namespace, and
OpenFGA alone authorizes it: no list of accounts is rendered. Renders nothing with auth off.
Call: include "lance.saVerifier" (list $root "env"|"volumes"|"mounts" <door>).

## `{{- define "lance.saFetch" -}}`

The API-audience token and cluster CA a reader of the SA issuer's discovery and keys fetches with, at
/var/run/secrets/rask/sa-fetch. Call: include "lance.saFetch" "volumes"|"mounts".

## `{{- define "lance.oidcIssuer" -}}`

The estate's one trust anchor, `auth.oidc.issuer`: every governed door's RASK_OIDC_ISSUER and every
zone's OIDC_ISSUER render from it, so the browser and the doors cannot be configured with two issuers
([[XC-148]]). Without the development Dex it is the platform's IdP, and the doors fetch the keys they
check each token against from it, so an http issuer is refused at render: over http, whoever answers
that address on the path chooses the keys.

## `{{- if $root.Values.dex.enabled }}`

Split-horizon, for the development Dex only: the issuer is the URL the browser sees and tokens carry;
discovery and JWKS are fetched in-cluster at `http://<release>-dex`. Without Dex (every real
deployment, prod-credentials.yaml) the doors fetch from the issuer itself.

## `- { name: RASK_OIDC_ALLOW_INSECURE, value: {{ ternary "true" "false" $root.Values.dex.enabled | quote }} }`

The http escape hatch opens exactly when the development Dex is the IdP, because its in-cluster
discovery URL is http whatever the issuer's own scheme. Keyed to the issuer's scheme instead, a
production render that left the issuer at a dev default trusted `rask-dex` over http on every door
([[XC-148]]). Stated as `"false"` otherwise rather than left to the setting's default, so the render
itself shows the HTTPS guard on.

## `- { name: RASK_OIDC_CONNECTORS, value: {{ include "lance.oidcConnectors" $root | quote }} }`

The idp id each Dex connector's users are principals of ([[LH-063]], ADR 0159 D5), as JSON, on every governed door. A
door keys a person on `<idp-id>~<user id>` from the token's `federated_claims` and refuses a token whose connector this
map does not name, so every door must carry the same map: one include, here.

## `{{- define "lance.oidcConnectors" -}}`

`auth.oidc.connectors`, plus the development Dex's one connector, its password database `local`, as `dex` when
`dex.enabled` and the map names no `local` of its own. Refused at render when empty, since a door would then sign
nobody in; for an empty connector id; and for an idp id that is not a string, is outside lower-case letters, digits and
`-`, or is `kubernetes`, which is the service-account issuer's: a person named `kubernetes~…` would read as a machine at
every door. The service refuses the same map at boot (`GovernedAuthSettings`, a `dict[str, str]`, which takes no number
for an idp id); the render refusal names the value before any pod starts. The compute fixture's copy of the map is
refused by the same rules (`deploy/fixtures/compute/chart/templates/compute.yaml`).

## `{{- define "lance.bootstrapAdmin" -}}`

`auth.bootstrapAdmin`, which the bootstrap-admin hook grants the estate's two standing authorities, must be a person's
principal, `<idp-id>~<user id>`. A bare name is refused at render because no token could ever prove it, a `user:` id
because the hook encodes the principal itself (`fga.user_object`), and a `kubernetes~` principal because a machine
administers no tenant (LH-433).

## `{{- define "lance.governedFgaEnv" -}}`

The FGA client coordinates. Separate from OIDC — see above.

## `{{- define "lance.openfgaAudience" -}}rask-openfga{{- end -}}`

What an OpenFGA client presents ([[XC-077]]): its projected token for the audience OpenFGA accepts (values.yaml
`openfga:`). Every pod that names OpenFGA renders all three parts under the condition its URL has. Call:
include "lance.fgaToken" (list "env"|"volumes"|"mounts").

## `{{- define "lance.openfgaSubjects" -}}`

The ServiceAccounts OpenFGA admits (`authn.oidc.subjects`), comma-joined: exactly the pods that render
`lance.fgaToken`, under the same conditions, qualified by the release namespace.

## `{{- define "lance.governedFgaPins" -}}`

The operator-pinned FGA store/model ids — optional, and emitted at DIFFERENT positions by
different callers relative to their own service-specific vars, which is why they are not folded
into the block above: doing so would reorder a caller's env list, a change this refactor has no
business making. The store id is the SAME auth.fgaStoreId the bootstrap-admin Job grants into,
so a pinned store can never diverge from the store the services check against. Unset =
provision-by-name at boot (`lance-catalog`).

## `{{- define "lance.corpusVolume" -}}`

---------------------------------------------------------------------------------------------
THE CORPUS VOLUME — one definition, because a writer and a reader that disagree are INVISIBLE.

Measured 2026-08-06, and it had been true for three days: `job/rask-seed-corpus` really did
seed a corpus (10 chunk rows, 3 documents, an FTS index over chunks.text) and the viewer really
did answer `{"datasets": []}`. Neither was broken. They mounted a volume with the same NAME and
a different SOURCE:

    seed job                  corpus -> hostPath /home/gabriel/media-corpus
    viewer/search/annotator   corpus -> emptyDir {}          (explorer.corpus.mode default)

A THIRD path was in play: explorer.corpus.hostPath defaults to /var/media-corpus, so even
flipping mode=hostPath would have missed the seeded bytes. Three paths, one volume name.

Nothing could catch it. The seed exits 0 (it wrote its files). The viewer is 1/1 Ready and
answers 200 (it read its directory). `helm template` renders. Every probe is green and the
product is empty — the failure only exists in the RELATION between two manifests, which is
exactly the thing no single manifest can assert.

So the volume source is defined ONCE, here, and every mount site includes it. A new writer
cannot pick its own source without deleting this call, which is a visible edit rather than a
silent divergence.

Modes are unchanged (emptyDir | pvc | hostPath) and so is their rationale — see values.yaml
`explorer.corpus`. What changes is that they are no longer transcribed per site.
---------------------------------------------------------------------------------------------

## `{{- define "lance.bootstrapJobDeadline" -}}`

lance.bootstrapJobDeadline — the wall-clock ceiling on a bootstrap Job.

WHY EVERY WAITING JOB NEEDS ONE, measured rather than argued. `backoffLimit` counts POD failures, and
these Jobs wait on a dependency with a loop. If the dependency never answers and the loop is unbounded, it never exits, so the container never exits, so the pod
never fails, so `backoffLimit` never counts and `ttlSecondsAfterFinished` never fires (a TTL applies
only after completion). The Job runs forever. Observed on the live estate 2026-09-24:
the scoped-users Job of revision r229 Running 27h, its pod still printing its wait line, provisioning nothing.

`activeDeadlineSeconds` is the only one of the three that bounds a pod that is HEALTHY and stuck, so it
is the backstop the other two cannot be. Thirty minutes is deliberately generous — a bootstrap that has
not finished in that long is not slow, it is stuck — so it fails no deploy that was going to succeed.

OBSERVED DOING IT, not assumed: a probe Job on this cluster carrying `until false; do echo wait-forever;
sleep 3; done` — r229's exact shape — and a 20-second deadline was terminated by Kubernetes with
`Failed=DeadlineExceeded` (2026-09-24). The three-bound reasoning above is only worth anything if the
one bound that applies actually fires here.

## `{{- define "lance.baseSecretField" -}}`

The field a data base's credential reference carries its S3 secret in (`catalog.multibase.baseCredentialRefs`,
[[LH-067]]), beside `aws_access_key_id`. It is the one static pair the catalog still reads, parked as [[XC-168]]; the
catalog's own identity holds no key (ADR 0170).

## `{{- define "lance.objectStoreAudience" -}}`

The audience the catalog's store token is projected for, `objectStore.identity.audience` (ADR 0170). One value names
both halves: the token the catalog's pod mounts (`lance.identityTokens`) and the client id the store trusts as its
identity provider's (`deploy/fixtures/rustfs` `identityProvider.clientId`, which
`tests/unit/test_a_provisioned_identity_is_one_the_service_uses.py` holds equal). `required`: a blank audience would
leave the catalog no identity at the store, and there is no key pair to fall back to.

## `{{- define "lance.mediaIdentity" -}}`

The corpus reader's access key, `objectStore.mediaAccessKey`, when explorer serves a corpus
(`explorer.enabled` and `explorer.corpusS3Root`), else empty. Every consumer branches on this one value: the
scoped-users hook's `rask-media` user and policy, the seed's `secret/media-s3`, and search's
`MEDIA_S3_ACCESS_KEY_ID` (explorer.yaml), so the user the store holds, the secret the store's seed writes and the
key a pod presents cannot disagree. `required` while a corpus is served: a blank key would leave the reader with
no identity, and it never falls back to the root (ADR 0168).

## `{{- define "lance.backupIdentity" -}}`

The backups' access key, `objectStore.backupAccessKey`, when either backup CronJob is on
(`backups.pgDump.enabled` or `backups.controlRoot.enabled`), else empty. One value gates the `rask-backup` user and
policy, the seed's `secret/backup-s3`, ESO's read of it, the `<release>-backup-s3` Secret and both CronJobs'
`AWS_ACCESS_KEY_ID`. `required` while a backup is on, so a backup never presents the store's root.

## `{{- define "lance.baseFixture" -}}`

[[LH-067]] The local data-base fixture (values.yaml `catalog.multibase.devSeed`): JSON
{ref: [{bucket, prefix}]} over `baseCredentialRefs`, `{}` when off.

## `{{- define "lance.scopedSecretFn" -}}`

The secret half of a SCOPED STORAGE IDENTITY (and of a per-base fixture credential), derived in the
cluster: `scoped_secret <identity> <file>` writes sha256("<identity>-s3-<root>")[:40] to <file> from the
store's root secret in `$root`, never to output, argv or the render.

WHY DERIVED. The two halves of a scoped pair are made by two pods, the dev OpenBao's seed (the field a
service reads) and `object-store-scoped-users` (RustFS `add-user`), and they agree only by computing the same
function of the same inputs. Both read the root from a file, so naming an identity in a values file is
the whole setting and no secret is a value ([[XC-004]]). Deterministic, so a re-run is not a rotation;
rotating the root rotates every derived secret with it.

## `{{- define "lance.storeReads" -}}`

WHO READS WHAT FROM THE STORE, ONE TABLE ([[XC-165]], ADR 0169): app id -> {its ServiceAccount, `api` (the secrets its
code reads through its sidecar), `components` (the secrets its own components resolve: its bus user, the state store's
DSN, a notification channel's credential)}. Every place that decides a read is a view of it: the app's Configuration
allows `api` (`lance.daprAppSpec`), its store role's policy grants `api` and `components` (the seed's
`policy-<release>-<app>.json`), and an app with neither reads nothing, so it gets no store at all
(`lance.secretScopes`). Two lists that agreed when written are how a read gets refused at runtime, or granted past
its owner, so nothing restates it.

The split is by READER SET, because a store and Dapr both grant by secret name: a secret two apps read is one secret
both name (`dapr-app-token`, `dapr-state`, `media-s3`), never a copy, so a rotation writes one place. The verifier
lists follow the env the same flags render (`LINEAGE_SIGNERS` under `signing.enforce`, the doors under
`signing.doors`), and a signer reads its own pair. The pub/sub condition restates `dapr-component.yaml`'s, so
`tests/unit/test_a_dapr_component_names_a_secret_something_can_resolve.py` checks, over every overlay CI deploys, that
each scoped sidecar's own policy reads every secret its components name.

## `{{- define "lance.esoReads" -}}`

What ESO's role reads: exactly the secrets the chart's ExternalSecrets (and the NATS fixture's, through rask's
SecretStore, `lance.natsFiles`) name, under the same conditions that render them. No app's secret is here.

## `{{- define "lance.storeRole" -}}`

`<release>-<app id>`: the store role each sidecar logs in as. The component names it with Dapr's `{appID}` tag, so
one component serves every app and each sidecar fills in its own id; the release prefix keeps two releases apart in
one platform store.

## `{{- define "lance.storePolicy" -}}`

A store policy as JSON, which OpenBao reads as HCL: read on each secret's KV v2 data path, and the token's own
lookup and renewal, which a role with no default policy needs for daprd to renew its session. Nothing else: no list,
no metadata, no write.

## `{{- define "lance.storeIdentity" -}}`

The token a sidecar logs in with, for an app that reads the store: the pod asks the kubelet for a ServiceAccount
token for `openbao.identity.audience` (600 s, rotated by the kubelet, bound to the pod) and `dapr.io/volume-mounts`
mounts it into daprd ALONE, at the path the component names in `vaultServiceAccountTokenPath`. No app container
mounts it, so an app's code cannot log in past its sidecar's Configuration. Both halves render from the one
membership, because an annotation naming a volume the pod lacks is a pod the API server refuses.

## `{{- define "lance.storeAudience" -}}`

The one audience a sidecar's store login is bound to, read by the projected token (`lance.storeIdentity`) and by every
app's role (secret-store-identities.yaml). A blank one fails the render: a role with no audience admits any token of its
ServiceAccount, so the login would no longer be daprd's alone.

## `{{- define "lance.secretScopes" -}}`

The app-ids `lance-secrets` admits: every app with a read in `lance.storeReads`. It has several readers that must
not drift: the Component's `scopes:` is what daprd enforces, `lance.appTokenEnv` below decides a pod's credential
path from the same membership, the per-app Configuration and the store token's volume follow it, and the
NetworkPolicy admits exactly these pods to the store. A pod told to read the store while its app-id is absent from
`scopes:` gets ERR_SECRET_STORES_NOT_CONFIGURED and fails closed at boot.

Usage: {{ splitList "," (include "lance.secretScopes" .) }}

## `{{- define "lance.appTokenEnv" -}}`

A workload's app-API-token credential: the STORE when the pod can reach it, the env row when it cannot.

THE ENV ROW IS THE BANNED PATH AND THIS IS WHERE IT ENDS. `APP_API_TOKEN` as a `secretKeyRef` is a
Kubernetes Secret injected as an environment variable, which the estate's rule refuses outright
(owner, verbatim: *"Never secret through envs. Either from ESO, secret store dapr and STS for zero
trust."*). A pod that already carries a Dapr sidecar and is in `lance.secretScopes` has the sanctioned
path IN it — `GET /v1.0/secrets/lance-secrets/lance` — so what ships here is which source to read,
which is configuration, while the value stays in OpenBao. `expected_app_tokens()` is the one accessor
all three doors go through (`require_dapr_token`, `service_principal`,
`assert_app_token_configured`), so the switch moves every door on the pod together.

THE FALLBACK IS NOT A FALLBACK CHAIN, and the distinction matters because the rule bans those too. It
is a RENDER-TIME branch on a fact that is fixed at deploy: either there is a Vault to read and this
app-id may read it, or there is not. The pod gets exactly one credential path and no runtime choice
between them — `expected_app_tokens()` reads the store or reads env, never one then the other.

A pod OUTSIDE the scope list still gets the env row on purpose. Flipping it without adding its app-id
to `lance.secretScopes` would make `assert_app_token_configured` fail its boot fetch and crash-loop
the service: loud, but a service down. Widening the scope is the fix; this branch is what keeps the
two edits from being separable.

Usage: {{- include "lance.appTokenEnv" (list $root "catalog") | nindent 12 }}

## `{{- define "lance.allocatorEnv" -}}`

The glibc allocator bound every lakehouse container carries.

WHY IT IS NOT A DEFAULT WORTH INHERITING: glibc sizes its arena cap from `sysconf(_SC_NPROCESSORS_ONLN)`
— the HOST's core count — and a cgroup CPU *quota* does not reduce visible CPUs. Measured 2026-09-21 in
these containers, `nproc` reads 64 while `cpu.max` reads `100000 100000` (one CPU), and the four
lakehouse pods held 34/41/42/59 secondary arenas of 64MB each. Each arena keeps its own free lists and
is trimmed independently, so RSS settles at the SUM of per-arena high-water marks and climbs until every
arena has seen its worst case — which is how `rask-maintenance` reached its 512Mi limit with a flat
Python heap and a Lance session at 7% of its cap.

MEASURED, NOT ASSUMED, in the image these pods run (Debian glibc 2.41): 64 threads forcing arena growth
reserved 65 arenas unset and 1 at `MALLOC_ARENA_MAX=2`. Two rather than one because a single arena
serialises every allocation in a 100+ thread process on one lock.

NOT A SECRET: a libc tunable with no confidentiality, read by glibc at startup and by nothing else.
Pinned by `tests/unit/test_the_lakehouse_bounds_its_allocator_arenas.py`.

## `{{- define "lance.daprConfigSpecBody" -}}`

The Dapr Configuration spec body, rendered identically into `lance-tracing` and into every
per-app-id `lance-config-<app>-<hash>` ([[XC-072]]). Extracted rather than copied: each stanza is a
measured decision, and a second copy would drift without anything saying so.

## `features:`

UNCONDITIONAL, and deliberately outside the otel guard below: this loop runs whether or not
telemetry ships.

Dapr 1.18 promoted HotReload to GA and default-ON, and every sidecar in this estate references
this Configuration — so each one boots with `Enabled features: HotReload …` and runs a 60-second
reconcile ticker that diffs the operator's components against its own in-memory store.

For `lance-statestore` that diff can never come out equal. Its DSN arrives as a `secretKeyRef`,
so the operator's copy holds the REFERENCE while daprd's holds the RESOLVED value; the reconciler
therefore concludes it changed on every single tick, tries to hot-reload it, and refuses —
because Dapr will not hot-reload a component used as an actor state store. Measured across the
fleet: ~540 ERROR lines an hour, `Aborting to hot-reload a state store component that is used as
an actor state store: lance-statestore`, one per sidecar per minute, forever. Nothing is pending
and nothing is broken; the loop is structurally unable to converge, and it buries real errors.

Turning it off costs nothing this estate was using. A component change here is a coordinated
chart rollout, and for the actor state store a pod restart was ALREADY required — that is the
very thing the reconciler was refusing to do for us. The trade is explicit: with HotReload off,
a Component/Resiliency/Configuration edit reaches running sidecars only after
`kubectl rollout restart`.

## `tracing:`

CORRECTED 2026-08-22. This block used to say "There is NO otel exporter here … Dapr's own sidecar
spans can't carry GreptimeDB's required db-name/pipeline headers", and concluded the estate still
had an end-to-end trace. Both halves were wrong.

`samplingRate` is a SAMPLING probability, NOT an enable switch. With no `otel:`, no `zipkin:` and
no `stdout:`, daprd registers its NullExporter: every sidecar span is created, sampled at 100%,
has its context propagated — and is dropped by an `ExportSpans` that returns nil. No log, no
error, no metric. Indistinguishable from a collector that is down. So NO Dapr hop in this estate
had ever produced a span: not service invocation, pub/sub publish or delivery, actor calls, input
bindings or workflow steps.

The stated reason was false twice over. Dapr's `otel.headers` accepts arbitrary {name,value} (and
secretKeyRef) pairs, so it CAN carry vendor headers — and it does not need to, because this chart
deploys a Collector that adds them itself. That Collector's `otlp` receiver already listened on
4317 and 4318 and was already wired into the traces pipeline.

Gated on otelEnabled, the one predicate every plane's export reads.

## `endpointAddress: {{ . | quote }}`

A BARE host:port. Dapr appends no URL path, so `protocol: http` posts to
`<endpointAddress>/v1/traces` — exactly the Collector's OTLP/HTTP route on 4318.

## `isSecure: false`

LOAD-BEARING: isSecure DEFAULTS TO TRUE upstream. Omit it and every sidecar attempts TLS
against a plaintext in-cluster Collector and fails — which fails soft, so the symptom is
"still no traces" rather than an error anyone sees.

## `protocol: http`

Exactly "http" or "grpc". Anything else is a fatal daprd startup error, not a fallback.

## `metrics:`

SIDECAR METRIC CARDINALITY. Gated on `dapr.enabled` only, deliberately NOT on
`lance.otelEnabled` — the same argument as the retention stanza below. The sidecar's :9090
exposition and its log stream exist whether or not this estate ships a collector, so a bound
that disappears with telemetry is not a bound.

`enabled` is REQUIRED by the CRD (chart/charts/dapr-1.18.1.tgz -> crds/configuration.yaml,
`required: [enabled]` under spec.metrics); omitting it is refused with
`spec.metrics.enabled: Required value`.

PLURAL `metrics:` ONLY. The CRD also accepts a legacy `metric:` whose schema is byte-identical
and states no precedence; daprd resolves it in Go by merging plural over singular field by
field, so writing both would make that merge the only tiebreak and the manifest would no longer
say what is in force.

NOTHING VALIDATES THESE FIELD NAMES BEFORE RUNTIME. Helm does not request strict field
validation, so `recordErrorCode`, `increasedCardinallity` or `obfuscateUrls` would render, apply
CLEANLY, and be pruned by the API server with no error and no warning — "the setting had no
effect", the same silent shape as the NullExporter above. Pinned by tests/unit/test_invariants.py
::test_the_sidecars_non_telemetry_config_survives_telemetry_being_off, which is the only gate
that fails loudly on a typo here.

## `recordErrorCodes: true`

dapr_error_code_total — a closed set of daprd error codes, so bounded by construction.

## `increasedCardinality: false`

THE POINT OF THIS BLOCK. At the upstream default (still `true` in 1.18.1) the sidecar stamps the
RAW remainder of a service-invocation URL onto the `path` label. Measured live on this cluster:
dapr_http_server_request_count{app_id="gateway",path="/v1.0/invoke/compute/method/api/ray/jobs"}
An invoked catalog or lineage path carries its table, namespace and project ids, so that is one
series per id, forever, in a store nothing authorizes reads against.

NOT A SUBJECT LEAK, and do not describe it as one. daprd 1.18.1 ALREADY templates actor ids
(`/v1.0/actors/InboxActor/{id}/method`) and drops state keys (`/v1.0/state/lance-statestore`) at
the default — verified by scraping a live sidecar, by an active probe with a subject-shaped key,
and by a real non-templated actor id present in app logs but absent from the metrics body. The
OIDC subject never reached a metric label. The rule in notifications/api/metrics.py is correct;
this is not an instance of it.

## `pathMatching:`

With increasedCardinality false the `path` label is the MATCHED pattern or the empty string, so
these three RESTORE labels that would otherwise be erased outright. Detail is still lost on
purpose: actor type, pubsub topic and store name collapse into their wildcards. Nothing in this
repo queries dapr_http_server_* (the dashboards and alerts use the component/actor/scheduler
families and the app SDK's own http_server_duration_milliseconds_*), so no consumer breaks.

NO `/v1.0/invoke/...` ENTRY, EVER. daprd auto-registers an invoke twin for every non-invoke
pattern here and registers the lot on a real http.ServeMux, which PANICS at startup on
conflicting patterns. A catch-all does not conflict with THIS list, but panics the moment anyone
adds `/` or a bare `/{x...}` to it. One Configuration serves every app-id, so that is the whole
fleet crash-looping on a one-line edit three lines above, with no recover() on the path and no
validation anywhere before runtime. It also buys nothing: it only renames the "" bucket. Invoke
paths therefore label as "", and an invoke hop is read from the UPSTREAM's own
http_server_duration_milliseconds_* instead — a deliberate erasure, not an oversight.

## `logging:`

NOT SET, and it is a decision rather than an oversight. `latencyDistributionBuckets` is GLOBAL:
workflow-execution latency shares the HTTP distribution, so one bucket list has to cover both.
Dapr's default 34 buckets top out at 100s, and a workflow that blocks on
`ctx.wait_for_external_event` (a person answering) is unbounded BY DESIGN, so no list covers it.
Consequence to know before trusting a chart: the workflow p95 panel renders +Inf for such a
lane. Setting a wider ceiling trades a visibly-broken quantile for a silently-wrong one.

## `obfuscateURLs: true`

INSEPARABLE from `enabled` above. With obfuscateURLs false daprd logs the raw
`method + URL.Path`, and the actor URL DOES carry base64url(<oidc sub>) as a path segment
(service_kit/governed/user_state.py::encode_subject, reversed exactly by decode_subject). With it
true daprd logs the matched endpoint NAME instead. This is the one place in this object where a
subject could reach telemetry, so the two keys ship together or not at all.

## `omitHealthChecks: true`

The kubelet polls /v1.0/healthz on every sidecar; without this the stream is mostly probe noise.

## `{{- range $k, $v := (dict "completed" .completed "failed" .failed "terminated" .terminated) }}`

A FLOOR, because the failure mode is silent and total. `completed: "0s"` is a valid duration
that renders, applies, and purges every workflow instance the moment it terminates — the
history is gone before anyone can read it, and nothing reports that. A typo in a values file
should not be able to do that, so anything under an hour is REFUSED at render rather than
discovered later by its absence. Raise the floor deliberately if a shorter window is ever
genuinely wanted.

## `{{- define "lance.daprAppSpec" -}}`

One app-id's Dapr Configuration spec: the Configuration object and its name (`lance.daprAppConfigName`) both derive from THIS.

## `defaultAccess: deny`

DENY BY DEFAULT ([[XC-165]]): the app's code reads exactly the secrets `lance.storeReads` lists for it as `api`, and
nothing a later secret adds. Dapr applies this scope only to the app-facing secrets API (`pkg/api/universal/
secrets.go:35`, `IsSecretAllowed`, the same in 1.18.1 and 1.19.0-rc.4); a component resolving its own `secretKeyRef`
goes straight to the store with the sidecar's login (`pkg/runtime/processor/secret/secret.go:162-170`), so a bus user
or the state store's DSN is resolved for the component and never served to the app. What the store itself refuses is
the role's policy, the second layer, which holds even for a request that never asks the sidecar.

Every secret a reader names is known at render: the viewer's per-store credentials come from the store registry,
`RASK_STORES`, which the chart renders from `storage.stores`, and a data base's credential reference from
`catalog.multibase.baseCredentialRefs`; a warehouse record's `credential_ref` is consumed by no door ([[LH-205]]).

## `allowedSecrets:`

Rendered only when the app reads something itself. An app whose components alone read the store (flows: the state
store's DSN) gets a deny-by-default scope with no list, which Dapr reads as every secret refused; a bare
`allowedSecrets:` over an empty range would be YAML null, which the CRD refuses (measured 2026-09-24 with
`deniedSecrets`).

## `{{- define "lance.daprAppConfigName" -}}`

Named by the hash of its spec, so an edit renames it; the object and the pod's `dapr.io/config` both use this ([[LH-064]], values.yaml `signing:`).

## `{{- define "lance.signers" -}}`

EVENT SIGNING ([[LH-064]]; values.yaml `signing:`). `lance.signers` is the ONE declaration, an (identity, key name, app-id,
control-event role) per signer: the mint and seed loops, the store's reads (`lance.storeReads`), RASK_SIGNING_IDENTITY, LINEAGE_SIGNERS and the doors' env
are views of it. The identity is the signer's service-account subject (lance.saSubject), the author its events are authorized as;
the key name is its ServiceAccount's name, which `service_kit.governed.signing_key.signing_key_name` derives from the identity,
because a subject's `~` and `:` belong in no secret name, OpenBao path or shell word.

## `{{- define "lance.signingKeys" -}}`

The key names the store holds a pair under (list $root [$appId]): every signer's, or one app-id's. Each must be a DNS label.

## `{{- define "lance.delegatorIdentities" -}}{{- include "lance.saSubject" (list . "catalog") -}}{{- end -}}`

Who may sign for a person: the catalog, which authenticated that person.

## `{{- define "lance.signingIdentitiesForApp" -}}`

The identities one app-id signs as (list $root $appId).

## `{{- define "lance.signingEnv" -}}`

RASK_SIGNING_IDENTITY for one Deployment (list $root $appId): one env row when the store is on and that app-id signs.

## `{{- define "lance.controlSignerRoles" -}}`

Every control-event role -> its identities (values.yaml `signing.doors`).

## `{{- define "lance.doorEnv" -}}`

A door host's env (list $root $takesControlEvents [$signerRoles]); values.yaml `signing.doors`.

## `{{- define "lance.signingShapes" -}}`

The shape checks the mint and seed scripts share: an NKEY user seed is SU + 56 and a public key U + 55 base32 characters.

## `{{- define "lance.platformBuckets" -}}`

The PLATFORM buckets, comma-joined: `objectStore.buckets`. The scoped-users hook
creates them on the dev store and `orphan_buckets` exempts them, both from here.

## `{{- define "lance.maintenancePlatformBuckets" -}}`

Buckets `orphan_buckets` must never report, as ONE expression both maintenance deployments read.
The category's contract is that a clean report certifies the estate, so a finding no operator can
action holds the total above zero forever and blocks the trash purge with it — the config's own
comment records `rask-observability` doing exactly that until `objectStore.buckets` was wired in.

A MULTIBASE DATA BASE IS THE SAME KIND OF THING and was missed: `catalog.multibase.dataBases` is an
estate DECLARATION that a bucket exists and may be addressed, and no warehouse record claims it, so
the reconcile called it an orphan. Measured live 2026-09-24: `orphan_buckets: 'lh067-second-store'`
— declared in this repo's own `values-local.yaml` — was one of the two findings holding
`trash_purge_blocked`. Declared here rather than in each template, because two copies of a bucket
list is how the first one drifted.

## `{{- define "rask.rayDashboardUrl" -}}`

THE ONE RAY PLANE ([[CP-041]]): the dashboard every consumer names (the zones' Serve origin;
the compute and annotator fixtures are given the same address as their own values). `ray.dashboardUrl`
when set, else empty.

## `{{- define "rask.rayServeOrigin" -}}`

Ray Serve's HTTP ingress on the same plane: `frontend.serveUrl` when Serve lives elsewhere; a RayService's
load-balanced serve Service; else the dashboard itself, whose external host serves the apps too.

## `{{- define "rask.apiRoutes" -}}`

THE ONE ROUTE TABLE: the public `/api/*` prefixes the cluster route publishes, as a JSON list of
`{prefix, component, service, port, rewrite}`. `route.yaml` renders one HTTPRoute rule per row
(`rewrite` becomes a `URLRewrite` `ReplacePrefixMatch`; empty forwards the path unchanged), and
`network-policy.yaml` renders one `<release>-edge-<component>` policy per row admitting that port, so a
published prefix and an admitted port come from one list and cannot drift.

The rows mirror the gates of the templates that render each backend: the media trio only under
`explorer.enabled` (explorer.yaml), catalog and lineage always (services.yaml), flows and notifications
only when front-door or single-tenant (fleet.yaml's own condition), so the route never names a Service
that did not render. The more specific `/api/explorer/*` rows precede `/api/explorer` for the reader;
Gateway API matches the longest prefix regardless of order.

A new public prefix is a row here plus a row in
`tests/unit/test_the_cluster_route_names_one_backend_per_api_prefix.py`. Compute's `/api/ray` and
`/api/serve` are not rows: the compute fixture chart publishes them with its own HTTPRoute.
