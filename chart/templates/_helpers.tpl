{{/* =====================================================================================================
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
deliberate (docs/architecture/lance-ns-merge.md: lance object names become `rask-*`), and `lance.fullname`
is kept on `.Release.Name` rather than delegated to `rask.fullname` because ~40 grafted lance templates
build sibling object names from a RAW `{{ .Release.Name }}-…` (the `-dapr-app-token`, `-frontend-session`,
`-observability-s3`, `-openbao-token` Secrets, the `-pubsub-resiliency` Configuration). Delegating would
move the Deployments but not those, splitting the estate the moment release name ≠ fullnameOverride.
Install under any other release name and the two planes diverge — keep them equal.

LABELS: `rask.labels` emits app.kubernetes.io/name=rask, `lance.labels` emits =lance-ns. Kept distinct on
purpose: rask workloads select on {name,instance,component} (fleet.yaml/controlplane.yaml/frontends.yaml)
while lance Services select on {component,instance} only (services.yaml). Different `name` values are what
guarantee a rask selector can never adopt a lance pod inside the shared release.
===================================================================================================== */}}

{{/* ---------------------------------------------------------------------------------------------------
     rask — the base chart's helpers (fleet: gateway, compute, controlplane, frontends —
     the orchestrator died at P7a; core-api/search-api/volumes-api died in the R6/R20 wave).
     --------------------------------------------------------------------------------------------------- */}}

{{- define "rask.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rask.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "rask.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "rask.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "rask.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{- define "rask.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rask.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "rask.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "rask.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Component labels: pass (list . "<component>") */}}
{{- define "rask.componentLabels" -}}
{{- $root := index . 0 -}}
{{- $component := index . 1 -}}
{{ include "rask.labels" $root }}
app.kubernetes.io/component: {{ $component }}
{{- end -}}

{{/* Ray auth token (gate 7 / R3): explicit value -> lookup-pinned existing Secret -> random.
     The Secret data key is `auth_token` — the KubeRay operator-Secret convention
     (RAY_AUTH_TOKEN_SECRET_KEY), which is why rayservice.yaml's spec.authOptions.secretName can
     hand THIS chart-owned Secret to the 1.6+ operator verbatim (no key rename, and the operator
     skips generating its own Secret when secretName is set). */}}
{{- define "rask.rayAuthToken" -}}
{{- if .Values.ray.auth.token -}}
{{- .Values.ray.auth.token -}}
{{- else -}}
{{- $existing := (lookup "v1" "Secret" .Release.Namespace (printf "%s-ray-auth-token" (include "rask.fullname" .))) -}}
{{- if and $existing $existing.data (index $existing.data "auth_token") -}}
{{- index $existing.data "auth_token" | b64dec -}}
{{- else -}}
{{- randAlphaNum 32 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* The RAY_AUTH_MODE/RAY_AUTH_TOKEN env pair for FLEET consumers that talk to a token-authed
     Ray (the `rayClient` services — compute). Ray-cluster containers do NOT use this include:
     spec.authOptions (rayservice.yaml, kuberay >= 1.6.0) makes the operator inject the same pair
     into head/worker/autoscaler containers itself. No-op unless ray.auth.enabled — so every
     consumer flips with the ONE toggle and the secretKeyRef can never dangle (the Secret renders
     under the same gate in ray-auth-token.yaml; when externalSecrets.enabled the ESO-synced
     Secret carries the same name+key). Usage: {{- include "rask.rayAuthEnv" . | nindent 16 }} */}}
{{- define "rask.rayAuthEnv" -}}
{{- if .Values.ray.auth.enabled -}}
- name: RAY_AUTH_MODE
  value: "token"
- name: RAY_AUTH_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "rask.fullname" . }}-ray-auth-token
      key: auth_token
{{- end -}}
{{- end -}}

{{/* rask.minioAccessKey / rask.minioSecretKey are GONE (lance-ns-merge P4 RustFS unification):
     the ONE store's root credential is rustfs.accessKey/secretKey everywhere — the Tenant credsSecret,
     the fleet's AWS_*, infra-credentials, and the hooks all read that single pair. */}}

{{/* ── The ONE GPU signal: ray.gpuCount ────────────────────────────────────────────────────────────
     `ray.gpuCount` is the single fact every GPU-shaped decision in this chart derives from. It exists
     because a live kind run (docs/architecture/live-proof-2026-07-28.md, defects 3 + 6) needed FOUR
     manual overrides to make a GPU-less estate coherent — ray.gpuCount=0, config.RASK_SERVE_GPU_FRAC=0,
     ray.runtimeClassName="" and nvdp off — and getting any one of them wrong wedged the deploy with no
     diagnostic (the RayService stayed `Initializing`, so no stable head Service ever appeared and the
     compute zone read "Ray offline" beside a perfectly healthy head).

     Derived from it: the head's `num-gpus` rayStartParam + `nvidia.com/gpu` limit, the nvidia
     RuntimeClass (runtimeclass.yaml), the htrflow Serve actor's GPU fraction (rask.serveGpuFrac, fed to
     BOTH the fleet ConfigMap and the RayService serveConfigV2), and the Kueue ClusterQueue's
     nvidia.com/gpu nominalQuota. The ONE thing it cannot gate is the nvidia-device-plugin SUBCHART:
     Helm resolves `condition:` against a static values path and cannot compute `gpuCount > 0`, so that
     stays `nvdp.enabled` — kept honest by the fail-closed coherence guard in gpu-coherence.yaml. */}}

{{/* "true" when this estate has GPUs at all; "" (falsey) otherwise. */}}
{{- define "rask.gpuEnabled" -}}
{{- if gt (int .Values.ray.gpuCount) 0 }}true{{- end -}}
{{- end -}}

{{/* The htrflow Serve actor's GPU fraction, DERIVED — never the raw config value. `config.RASK_SERVE_GPU_FRAC`
     states the intent for a GPU node; on a GPU-less estate it MUST collapse to 0 or Ray Serve waits forever
     for a resource the cluster will never advertise, and the whole RayService (hence the stable head
     Service, hence the compute zone) never comes up. Both render sites include this, so the ConfigMap and
     the serveConfigV2 runtime_env cannot disagree. */}}
{{- define "rask.serveGpuFrac" -}}
{{- if include "rask.gpuEnabled" . }}{{ .Values.config.RASK_SERVE_GPU_FRAC }}{{ else }}0{{ end }}
{{- end -}}

{{/* Dapr sidecar pod annotations (no-op unless dapr.sidecars) — the ONE annotation surface for the whole
     estate: the rask fleet + controlplane AND the lance planes (services.yaml catalog/lineage,
     medallion.yaml producer/movers, compaction.yaml, media.yaml viewer/search/annotator) all render
     THIS helper, so the sidecar contract can never drift between planes (DAPRIFY 2026-07-27). Carries the full union of what the two planes shipped:
       - enabled / app-id / app-port / log-level (the original rask surface)
       - max-body-size (when dapr.maxBodySize is set — Dapr's 4Mi default rejects multi-image batch uploads)
       - app-token-secret → the shared <release>-dapr-app-token Secret (dapr-app-token.yaml, same
         dapr.sidecars gate, so the reference can never dangle): Dapr injects APP_API_TOKEN and stamps
         `dapr-api-token` on delivered requests → sidecar-only routes reject forged direct POSTs
       - the daprd resource bounds + disable-builtin-k8s-secret-store (+ optional seccomp) via
         lance.daprSidecarResources
       - dapr.io/config: lance-tracing — gated on lance.otelEnabled EXACTLY like the app containers' OTel
         wiring (observability.enabled OR externalOtlpEndpoint), so sidecar traces flip with the apps'.
     Usage: {{- include "rask.daprAnnotations" (list $root $appId $appPort) | nindent 8 }} */}}
{{/* ---------------------------------------------------------------------------------------------
     DAPR SIDECAR INJECTION — the second `helm install --wait` ordering defect (2026-07-28).

     Dapr's sidecar injector is a MUTATING WEBHOOK, and its MutatingWebhookConfiguration ships
     `failurePolicy: Ignore` by default. The Dapr control plane is a SUBCHART of this release, so on a
     fresh cluster the app pods are created in the same breath as the injector: the API server calls a
     webhook with no endpoints, SILENTLY admits the pod unmutated, and the app comes up with NO daprd
     container at all. Nothing recreates that pod — a CrashLoopBackOff restarts the container inside
     the SAME pod, which will never gain a sidecar — so the app crash-loops forever on
     "secret unavailable from Dapr store … failing closed" and `helm install --wait` times out. It only
     ever "worked" on a long-lived cluster where Dapr was already running.

     Fix: `failurePolicy: Fail`, scoped by an `objectSelector` to pods carrying this LABEL. Fail-closed
     admission makes the API server REJECT the create instead of silently dropping the sidecar; the
     ReplicaSet controller retries, and the first retry after the injector is Ready produces a properly
     injected pod. The objectSelector is what makes `Fail` safe: an unscoped fail-closed pod webhook
     would also block the injector's OWN pod (and every infra pod) and wedge the cluster permanently.

     The label must therefore appear on EXACTLY the pods that carry the `dapr.io/enabled` ANNOTATION —
     a pod with the annotation but no label is admitted unmutated again (the silent failure, restored),
     and one with the label but no annotation just costs an admission round-trip. That correspondence
     is mechanical, so it is a render guard:
     test_invariants.py::test_every_dapr_annotated_pod_carries_the_injector_webhook_label. */}}
{{- define "rask.daprPodLabels" -}}
{{- if .Values.dapr.sidecars -}}
dapr.io/enabled: "true"
{{- end }}
{{- end -}}

{{- define "rask.daprAnnotations" -}}
{{- $root := index . 0 -}}
{{- $appId := index . 1 -}}
{{- $appPort := index . 2 -}}
{{- if $root.Values.dapr.sidecars -}}
dapr.io/enabled: "true"
dapr.io/app-id: {{ $appId | quote }}
dapr.io/app-port: {{ $appPort | quote }}
dapr.io/log-level: {{ $root.Values.dapr.logLevel | quote }}
{{- with $root.Values.dapr.maxBodySize }}
dapr.io/max-body-size: {{ . | quote }}
{{- end }}
dapr.io/app-token-secret: {{ $root.Release.Name }}-dapr-app-token
{{- include "lance.daprSidecarResources" $root | nindent 0 }}
{{- if include "lance.otelEnabled" $root }}
dapr.io/config: "lance-tracing"
{{- end }}
{{- end }}
{{- end -}}

{{/* OTLP/OpenTelemetry container env for the rask FLEET (no-op unless observability.enabled). Shared by
     fleet + controlplane so the OTLP wiring never drifts.
     Usage: {{- include "rask.otelEnv" (list $root "service-name") | nindent 12 }}

     MERGE NOTE — this stays DIRECT-to-GreptimeDB (rask's shipped behaviour); the lance-ns apps export
     through the OTel Collector via "lance.otelEnv"/"lance.otlpEndpoint". Two paths, one store: the
     Collector's own exporter lands in the same GreptimeDB, so nothing is lost either way. What DID move
     here from lance-ns is the DATA: the release-derived Greptime host (was hardcoded "rask-greptimedb-
     standalone", which ignored the release name) and the observability.{greptimePort,dbName,tracePipeline}
     knobs, so the fleet and the lance apps cannot disagree about port/db/pipeline. `hasKey`+`ternary`, not
     `| default`, so an explicit 0 is never swallowed (and the chart-wide invariant test forbids it). */}}
{{- define "rask.otelEnv" -}}
{{- $root := index . 0 -}}
{{- $svc := index . 1 -}}
{{- $o := $root.Values.observability -}}
{{- if $o.enabled -}}
{{- $port := (hasKey $o "greptimePort") | ternary $o.greptimePort 4000 -}}
{{- $db := (hasKey $o "dbName") | ternary $o.dbName "public" -}}
{{- $pipeline := (hasKey $o "tracePipeline") | ternary $o.tracePipeline "greptime_trace_v1" -}}
- name: RASK_OTEL_ENABLED
  value: "true"
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: "http://{{ include "lance.greptimeHost" $root }}:{{ $port }}/v1/otlp"
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: "http/protobuf"
# Generic headers apply to metrics (and anything without a signal-specific
# override): db-name only — GreptimeDB ingests OTLP metrics with no pipeline.
- name: OTEL_EXPORTER_OTLP_HEADERS
  value: "x-greptime-db-name={{ $db }}"
# Traces additionally need GreptimeDB's trace pipeline; signal-specific
# headers override the generic ones for traces only.
- name: OTEL_EXPORTER_OTLP_TRACES_HEADERS
  value: "x-greptime-db-name={{ $db }},x-greptime-pipeline-name={{ $pipeline }}"
- name: OTEL_SERVICE_NAME
  value: {{ $svc | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "service.namespace=rask,deployment.environment=%s" $root.Release.Namespace | quote }}
{{- end }}
{{- end -}}

{{/* ---------------------------------------------------------------------------------------------------
     lance-ns — grafted verbatim from lance-ns main@083b49a chart/templates/_helpers.tpl.

     Every one of these is referenced by a grafted lance template (services.yaml, medallion.yaml,
     compaction.yaml, media.yaml, gateway.yaml, rustfs.yaml, openbao.yaml, dex.yaml, age-postgres.yaml,
     otel-collector.yaml, network-policy.yaml, ha.yaml, runners.yaml, …) or by a template still being
     merged by another owner (frontends.yaml → "lance.frontendEnv"/"lance.frontendImage"). None of them
     was renamed: no lance name collided with a rask name.

     VALUES THEY REQUIRE (the values.yaml merge must land these or the render breaks):
       age.externalHost · auth.enabled · catalog.controlEmit · dapr.{enabled,sidecars,sidecarRestricted,
       sidecarResources.*,resiliency.enabled} · dex.clientId · frontend.{apps,image.tag,serviceIdentity,
       idleTimeoutSeconds,oidc.*} · gateway.port · image.catalog.{repository,tag} · lifecycle.preStopSeconds ·
       medallion.{enabled,port,buckets,producer.daprAppId,movers} · nats.{enabled,externalUrl} ·
       observability.{enabled,dbName,tracePipeline,greptimePort,environment,externalOtlpEndpoint,
       otelCollector.{enabled,externalEndpoint}} · openbao.{enabled,port,externalAddr} · pubsub.name ·
       resources.{default,<component>} · rustfs.{bucket,port,externalEndpoint} · security.readOnlyRootFilesystem ·
       services.{catalog,lineage}.{port,daprAppId,reconcile.bindingName}
     `rustfs.bucket` (singular) is the one that is easy to lose: rask's values ship `rustfs.buckets` (a
     LIST) and lance's `lance.stageBucket` reads `rustfs.bucket` (a STRING) — both must exist.
     --------------------------------------------------------------------------------------------------- */}}

{{/* Release name is the fullname (install as `helm install rask ./chart` → all names = rask-*). */}}
{{- define "lance.fullname" -}}{{ .Release.Name }}{{- end -}}

{{/* ---------------------------------------------------------------------------------------------
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

     Jobs that depend on the APPS instead (bootstrap-admin needs a booted catalog; the Greptime TTL
     job needs a live GreptimeDB) stay post-install hooks — nothing waits on them, which is precisely
     what a post-install hook is for.

     A plain Job's spec is IMMUTABLE, so the name carries the release revision: an upgrade renders a
     new name (new Job, re-runs — the same semantics `before-hook-creation` gave the hooks), helm
     deletes the previous revision's Job because it left the manifest, and a re-render of the same
     revision is byte-identical (no patch, no immutability error).
     --------------------------------------------------------------------------------------------- */}}
{{- define "rask.bootstrapRev" -}}r{{ .Release.Revision }}{{- end -}}

{{- define "lance.labels" -}}
app.kubernetes.io/name: lance-ns
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* In-cluster hostnames (subcharts derive their service name from the release). */}}
{{- define "lance.ageHost" -}}{{ .Release.Name }}-age{{- end -}}
{{- define "lance.natsHost" -}}{{ .Release.Name }}-nats{{- end -}}
{{- define "lance.openfgaHost" -}}{{ .Release.Name }}-openfga{{- end -}}
{{- define "lance.dexHost" -}}{{ .Release.Name }}-dex{{- end -}}
{{/* The ONE object store's S3 host — the rustfs-operator Tenant's Service (`<tenant>-io`, Tenant CR
     `rask-rustfs` in templates/rustfs-tenant.yaml). RESOLVED per docs/architecture/lance-ns-merge.md
     P4 ("RustFS: rask's operator Tenant wins"): the first-party `<release>-rustfs` Service this used
     to name is deleted, so fleet + lakehouse + observability all resolve the same endpoint. */}}
{{- define "lance.rustfsHost" -}}{{ include "rask.fullname" . }}-rustfs-io{{- end -}}
{{- define "lance.openbaoHost" -}}{{ .Release.Name }}-openbao{{- end -}}
{{- define "lance.greptimeHost" -}}{{ .Release.Name }}-greptimedb-standalone{{- end -}}

{{/* App image refs. required() fails the render LOUD if the tag is empty/unset — e.g. a prod overlay
that forgot to set a release tag — instead of emitting an invalid `repo:` ref the kubelet only rejects
later (InvalidImageName), bricking the pod with no obvious signal. */}}
{{- define "lance.catalogImage" -}}
{{- $i := .Values.image.catalog -}}
{{- printf "%s:%s" $i.repository (required "image.catalog.tag must be set (a release tag in prod; `dev` locally)" $i.tag) -}}
{{- end -}}
{{/* A micro-frontend zone image: lance-<zone>:<tag> (built from the ONE parametrized frontend.dockerfile
via --build-arg APP=<zone>; `make frontend-images`). Call: include "lance.frontendImage" (list $root "data").

MERGE NOTE (frontends.yaml owner): kept VERBATIM, so it still renders the `lance-` image prefix, while
rask's frontends.yaml renders `"{{ .name }}:{{ $fe.image.tag }}"` (no prefix). The two image-naming schemes
are a real conflict and it is NOT settled in this file — docs/architecture/lance-ns-merge.md rules the
merged OBJECT names get the `rask-web-` prefix but says nothing about image names. Whoever merges
frontends.yaml picks one; the part worth keeping from here is the per-zone `tag` override + `required()`. */}}
{{- define "lance.frontendImage" -}}
{{- $root := index . 0 -}}{{- $name := index . 1 -}}
{{- /* A zone boundary costs a full document load, and the only thing it buys is INDEPENDENT DEPLOY —
       which a single shared tag silently cancels: every zone ships together, so the zones are pure cost.
       Per-zone `tag` in frontend.apps wins; frontend.image.tag remains the default so a whole-estate
       release stays one value, and a single zone can be rolled forward or back on its own. */ -}}
{{- $app := "" -}}
{{- range $root.Values.frontend.apps }}{{- if eq .name $name }}{{- $app = . }}{{- end }}{{- end }}
{{- $tag := "" -}}
{{- if $app }}{{- $tag = $app.tag | default $root.Values.frontend.image.tag -}}
{{- else }}{{- $tag = $root.Values.frontend.image.tag -}}{{- end }}
{{- printf "lance-%s:%s" $name (required "frontend.image.tag must be set (a release tag in prod; `dev` locally), or a per-zone `tag` on the frontend.apps entry" $tag) -}}
{{- end -}}

{{/* The SHARED env every micro-frontend zone gets — the cross-cutting "auth/secret similar in every MFE"
seam (mirrors the retired web pod's env, single-sourced here). Backend URLs the zones' BFF proxies target
directly (CATALOG_API/LINEAGE_API/MEDALLION_API/GREPTIME_API), the in-cluster gateway for the SSR /api
rewrite, and — auth-on — the service-cred READ fallback + (oidc-on) the OIDC config so EVERY zone reads the
shared origin-wide session cookie (home additionally exchanges the code). Emit under a container `env:`. */}}
{{- define "lance.frontendEnv" -}}
- { name: LINEAGE_API, value: "http://{{ include "lance.fullname" . }}-lineage:{{ .Values.services.lineage.port }}" }
- { name: CATALOG_API, value: "http://{{ include "lance.fullname" . }}-catalog:{{ .Values.services.catalog.port }}" }
{{- if .Values.medallion.enabled }}
- { name: MEDALLION_API, value: "http://{{ include "lance.fullname" . }}-lance-ray:{{ .Values.medallion.port }}" }
{{- end }}
- { name: GREPTIME_API, value: "http://{{ include "lance.greptimeHost" . }}:{{ (hasKey .Values.observability "greptimePort") | ternary .Values.observability.greptimePort 4000 }}" }
{{- if .Values.nats.enabled }}
# JetStream visibility (admin /streams): the NATS HTTP monitor port, unauthenticated by design and
# ClusterIP-only — consumed strictly server-side behind the zone BFF's admin gate, never by the browser.
# The headless Service is the one that carries :8222 (the plain Service exposes only 4222).
- { name: NATS_MONITOR_API, value: "http://{{ include "lance.natsHost" . }}-headless:8222" }
{{- if and .Values.dapr.enabled .Values.dapr.sidecars }}
{{/* Dead-subscription detector (admin /streams): the comma list of "STREAM:service" consumer groups the
estate EXPECTS on JetStream, rendered from the SAME values (and the same medallion.enabled /
catalog.controlEmit / dapr.resiliency.enabled gates) dapr-component.yaml and the *_DLQ_TOPIC envs render
their subscriptions from — so the expectation cannot drift from the real subscription topology. Gated on
dapr.sidecars too (not just dapr.enabled): components may render, but without injected sidecars no app
subscribes, and the panel would report every group as a false dead subscription. The zone BFF diffs this
against live /jsz consumers: an expected group that is absent (or present-but-unbound) is a silently-dead
subscription (a Ready pod reading nothing — the 2026-07-13 cascade stall), invisible in the raw monitor
payload. The catalog control consumer is group-less by design (broadcast); the BFF counts any no-group
ephemeral on CATALOG_CONTROL as the catalog. */}}
{{- $expected := list (printf "LINEAGE:%s" .Values.services.lineage.daprAppId) }}
{{- if .Values.dapr.resiliency.enabled }}
{{/* DLQ parking subscription (services.yaml LINEAGE_DLQ_TOPIC): lineage subscribes dlq.lineage.events
on its own component, so its group must be live on the DLQ stream whenever resiliency is on. */}}
{{- $expected = append $expected (printf "DLQ:%s" .Values.services.lineage.daprAppId) }}
{{- end }}
{{- if .Values.medallion.enabled }}
{{- $expected = append $expected (printf "LINEAGE:%s" .Values.medallion.producer.daprAppId) }}
{{- range .Values.medallion.movers }}
{{- $expected = append $expected (printf "MEDALLION:%s" .daprAppId) }}
{{- end }}
{{- $expected = append $expected (printf "TRAINING:%s" .Values.medallion.producer.daprAppId) }}
{{- if .Values.dapr.resiliency.enabled }}
{{/* DLQ parking subscriptions (medallion.yaml MEDALLION_DLQ_TOPIC, same resiliency gate): the producer
parks on dlq.lance-ray, each mover on dlq.<subTopic> — all queue-grouped by app-id on the DLQ stream. */}}
{{- $expected = append $expected (printf "DLQ:%s" .Values.medallion.producer.daprAppId) }}
{{- range .Values.medallion.movers }}
{{- $expected = append $expected (printf "DLQ:%s" .daprAppId) }}
{{- end }}
{{- end }}
{{- end }}
{{- if .Values.catalog.controlEmit }}
{{- $expected = append $expected (printf "CATALOG_CONTROL:%s" .Values.services.catalog.daprAppId) }}
{{- end }}
- { name: JETSTREAM_EXPECTED_CONSUMERS, value: {{ join "," $expected | quote }} }
{{- end }}
{{- end }}
{{/* MERGE FIX — the surviving gateway's port. docs/architecture/lance-ns-merge.md §decision 4: "rask's
FastAPI gateway (:8888, Dapr-aware) wins; lance-ns's nginx gateway retires (P1/P4)", and rask's gateway
carries the lance routes (/api/catalog, /api/lineage, /api/produce, /api/train, /api/explorer/*). The nginx
gateway (and its top-level `gateway:` values block, port 8080) is DELETED, so the fleet entry
(`services.gateway.port`) is the source of truth; 8888 is the last-resort fallback for a values file with
no fleet gateway (the port CLAUDE.md pins). */}}
{{- $gwPort := 8888 -}}
{{- with (index .Values.services "gateway") }}{{- $gwPort = .port }}{{- end }}
- { name: LANCE_GATEWAY_URL, value: "http://{{ include "lance.fullname" $ }}-gateway:{{ $gwPort }}" }
- { name: PORT, value: "3000" }
{{/* IDLE_TIMEOUT is read by svelte-adapter-bun's server bootstrap (dist/files/index.js:
`parseInt(env("IDLE_TIMEOUT", "10"))` → Bun.serve's idleTimeout, in SECONDS) and it is the FIRST thing
that severs a live query, well before the edge. Measured on kind 2026-07-26: with the ingress annotation
alone, `query.live` streams died every ~12s — nginx logged "upstream prematurely closed connection" with
upstream_response_time 12.001, i.e. the zone's own server hung up 10s after its last yield, not the proxy.
A generator that yields only on change is idle by design, so the default made every live subscription a
12-second reconnect loop: more traffic than the setInterval it replaced. Bun caps idleTimeout at 255s.

255 turned out NOT to be enough, and the note that used to sit here — that a feed outliving it needs a
keepalive rather than a bigger number — is refuted by measurement: the estate has a 20s keepalive and a
stream still died at 256.8s over a 290s hold. Bun's idleTimeout is not refreshed by outbound SSE writes;
for a streaming response it is a maximum connection LIFETIME. So 0 (disabled) is now the default, and the
old objection (a wedged client leaking a socket) is answered by that keepalive: the server writes every
20s, so a vanished peer fails the write and the generator ends.

`hasKey` rather than `| default`: Helm's `default` treats 0 as empty, so `| default 255` rendered 255 for
an explicit 0 and the change looked applied while nothing moved — the same trap already recorded for
booleans, biting an integer. Pair with ingress.annotations' proxy-read-timeout: both hops must hold, and
the SMALLER one always wins. */}}
- { name: IDLE_TIMEOUT, value: {{ (hasKey .Values.frontend "idleTimeoutSeconds") | ternary .Values.frontend.idleTimeoutSeconds 255 | quote }} }
{{- if .Values.auth.enabled }}
# Governed READ fallback: with no user session the BFF authenticates to lineage as a SERVICE (bounded by
# frontend.serviceIdentity's FGA READER rung), so the read-only UI works without a per-user browser login.
- name: LINEAGE_SERVICE_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ .Release.Name }}-dapr-app-token, key: token }
- { name: LINEAGE_SERVICE_ID, value: {{ .Values.frontend.serviceIdentity | quote }} }
{{- if .Values.frontend.oidc.enabled }}
# Per-user OIDC login (opt-in; needs a browser-reachable IdP). Every zone reads the sealed session cookie
# (ISSUER+CLIENT_ID+REDIRECT_URI make authEnabled true; SESSION_SECRET decodes it); the home zone also
# presents the confidential client secret at the token exchange. Secrets ride a Secret via secretKeyRef.
{{- if not .Values.frontend.oidc.sessionSecret }}{{ fail "frontend.oidc.enabled requires frontend.oidc.sessionSecret (>=32 chars) to seal the session cookie" }}{{- end }}
{{- if not .Values.frontend.oidc.publicIssuer }}{{ fail "frontend.oidc.enabled requires frontend.oidc.publicIssuer (a browser-reachable IdP)" }}{{- end }}
{{- if not .Values.frontend.oidc.publicOrigin }}{{ fail "frontend.oidc.enabled requires frontend.oidc.publicOrigin (the browser-reachable origin)" }}{{- end }}
- { name: OIDC_ISSUER, value: {{ .Values.frontend.oidc.publicIssuer | quote }} }
- { name: OIDC_CLIENT_ID, value: {{ .Values.dex.clientId | quote }} }
- name: OIDC_CLIENT_SECRET
  valueFrom:
    secretKeyRef: { name: {{ .Release.Name }}-frontend-session, key: clientSecret }
- { name: OIDC_REDIRECT_URI, value: "{{ .Values.frontend.oidc.publicOrigin | trimSuffix "/" }}/auth/callback" }
- name: SESSION_SECRET
  valueFrom:
    secretKeyRef: { name: {{ .Release.Name }}-frontend-session, key: secret }
{{- end }}
{{- end }}
{{- end -}}

{{/* CONSUMER endpoints — return the EXTERNAL override when set (the in-cluster component is then usually
disabled, e.g. a managed S3 / Postgres / Vault / collector in prod), else the in-cluster address. The
component's OWN Service/StatefulSet keeps the plain *Host helper above; only the apps that CONNECT switch.
This is what makes the docs/DURABILITY.md tier-3 externalization real (values-prod.yaml sets the overrides). */}}
{{- define "lance.s3Endpoint" -}}
{{- if .Values.rustfs.externalEndpoint -}}{{ .Values.rustfs.externalEndpoint }}{{- else -}}http://{{ include "lance.rustfsHost" . }}:{{ .Values.rustfs.port }}{{- end -}}
{{- end -}}
{{/*
lance.stageBucket — the S3 bucket for a medallion stage NAMESPACE, honouring the medallion→sink zone
model (R23: external raw is NOT a zone — ingest sources live outside the lakehouse). `medallion.buckets`
maps a namespace to its bucket; anything unset falls back to the shared `rustfs.bucket`. So gold
(SINK/output) can live in its own bucket/tenant while bronze/silver (the project's medallion internals)
stay in the project bucket — and the DEFAULT (no override) is the single-bucket layout, unchanged.
Call: {{ include "lance.stageBucket" (list $root "gold") }}.
*/}}
{{- define "lance.stageBucket" -}}
{{- $root := index . 0 -}}{{- $ns := index . 1 -}}
{{- $buckets := $root.Values.medallion.buckets | default dict -}}
{{- default $root.Values.rustfs.bucket (index $buckets $ns) -}}
{{- end -}}
{{- define "lance.ageConnectHost" -}}
{{- .Values.age.externalHost | default (include "lance.ageHost" .) -}}
{{- end -}}
{{- /* The OTLP endpoint the apps export to. OTel-first: the Collector is the default target. Priority:
external Collector (prod) > in-cluster Collector > a raw externalOtlpEndpoint (legacy) > GreptimeDB-direct.
The GreptimeDB-direct port is read through hasKey/ternary (not `| default`) so a values merge that drops
observability.greptimePort renders :4000 instead of a portless, silently-broken URL. */ -}}
{{- define "lance.otlpEndpoint" -}}
{{- $o := .Values.observability -}}
{{- $c := $o.otelCollector | default dict -}}
{{- $gp := (hasKey $o "greptimePort") | ternary $o.greptimePort 4000 -}}
{{- if $c.externalEndpoint -}}{{ $c.externalEndpoint }}
{{- else if $c.enabled -}}http://{{ include "lance.fullname" . }}-otel-collector:4318
{{- else if $o.externalOtlpEndpoint -}}{{ $o.externalOtlpEndpoint }}
{{- else -}}http://{{ include "lance.greptimeHost" . }}:{{ $gp }}/v1/otlp{{- end -}}
{{- end -}}
{{- /* Non-empty when the apps export THROUGH a Collector (external or in-cluster). In that case they send
plain OTLP and the Collector adds GreptimeDB's db-name/pipeline headers; only the direct-to-GreptimeDB paths
carry those headers on the app side. */ -}}
{{- define "lance.otelViaCollector" -}}
{{- $c := .Values.observability.otelCollector | default dict -}}
{{- if or $c.externalEndpoint $c.enabled -}}true{{- end -}}
{{- end -}}
{{/* Whether the apps should carry the OTel SDK wiring (instrument + otelEnv + the lance-tracing Dapr config).
Decoupled from `observability.enabled`: that flag deploys the IN-CLUSTER stack (GreptimeDB/OTel Collector/Perses), but
telemetry must also flow when it's OFF and `externalOtlpEndpoint` ships OTLP to an external collector (the OTel
operator path) — otherwise externalize silently emits nothing. Non-empty string = on (helm `if` truthiness). */}}
{{- define "lance.otelEnabled" -}}
{{- if or .Values.observability.enabled .Values.observability.externalOtlpEndpoint -}}true{{- end -}}
{{- end -}}
{{- define "lance.vaultAddr" -}}
{{- if .Values.openbao.externalAddr -}}{{ .Values.openbao.externalAddr }}{{- else -}}http://{{ include "lance.openbaoHost" . }}:{{ .Values.openbao.port }}{{- end -}}
{{- end -}}
{{- define "lance.natsUrl" -}}
{{- if .Values.nats.externalUrl -}}{{ .Values.nats.externalUrl }}{{- else -}}nats://{{ include "lance.natsHost" . }}:4222{{- end -}}
{{- end -}}

{{/* The per-SUBSCRIBER pubsub component name (call: include "lance.subPubsub" (list $root <appId>)).
Each subscriber app-id gets its OWN pubsub.jetstream component carrying its queueGroupName — one shared
component cannot: lineage AND lance-ray both consume lineage.events.v1, so a single queue group would
SPLIT those messages across the two apps instead of duplicating per app / competing per replica. The
component (dapr-component.yaml) and the app's *_PUBSUB env must agree on this name — hence one helper. */}}
{{- define "lance.subPubsub" -}}
{{- $root := index . 0 -}}
{{- $root.Values.pubsub.name }}-{{ index . 1 -}}
{{- end -}}

{{/* daprd sidecar resource annotations — the app containers are bounded (resources.default), so the
sidecars must be too (an unbounded daprd per pod × 8 pods can starve a small node). One helper = one
place to size them. */}}
{{- define "lance.daprSidecarResources" -}}
{{- $r := .Values.dapr.sidecarResources -}}
dapr.io/sidecar-cpu-request: {{ $r.cpuRequest | quote }}
dapr.io/sidecar-cpu-limit: {{ $r.cpuLimit | quote }}
dapr.io/sidecar-memory-request: {{ $r.memoryRequest | quote }}
dapr.io/sidecar-memory-limit: {{ $r.memoryLimit | quote }}
{{/* daprd AUTO-REGISTERS a built-in `kubernetes` secret store in k8s mode and initialises it at boot,
which builds a client from the pod's k8s-API SA token. We never use that store (our only secret store is
`lance-secrets` — secretstores.hashicorp.vault, scoped per app), but its init is FATAL on failure: with
security.serviceAccounts.enabled the per-workload SAs set automountServiceAccountToken=false, the token
file is gone, daprd falls back to `stat /home/nonroot/.kube/config`, and EVERY Dapr-injected pod
CrashLoops ("[INIT_COMPONENT_FAILURE] ... secretstores.kubernetes/v1" — live 2026-07-13; the SA flip was
unshippable). Disabling the unused store is the fix that KEEPS the audit's intent (no mounted JWT, zero
API grants) instead of walking it back by re-mounting the token. Always on: the store is unused with the
flag off too, so this also drops an unnecessary k8s-API surface from the default deployment. */}}
dapr.io/disable-builtin-k8s-secret-store: "true"
{{- if .Values.dapr.sidecarRestricted }}
{{/* PodSecurity `restricted` compliance for the INJECTED daprd sidecar (our app containers are already
compliant via lance.securityContext; the sidecar is not by default). RuntimeDefault seccomp is the per-pod
annotation; drop-ALL-caps is the injector-wide `dapr.dapr_sidecar_injector.sidecarDropALLCapabilities` value
(set it true TOGETHER with this flag — a subchart value can't read this one). OFF by default like
networkPolicy.enabled: full `restricted` ENFORCE is a prod posture, and on this stack it is additionally
BLOCKED by the OTel Collector — a single Deployment whose filelog receiver inherently needs hostPath
(/var/log/pods), which `restricted` forbids and no value fixes. The Collector needs its own namespace at
`baseline`, or a ServiceAccount
PSA exemption in the API-server admission config. So: this hardens what the chart owns; full-namespace
enforce stays parked-by-design (docs/KIND-RUNBOOK §6.4). Live-provable in isolation: flip this + the
injector value, re-roll, and the daprd container carries drop:[ALL] + RuntimeDefault. */}}
dapr.io/sidecar-seccomp-profile-type: RuntimeDefault
{{- end }}
{{- end -}}
{{/* Do the app services consume secrets via Dapr (the secret store), vs plaintext env? True when there is
ANY Vault to read from — the in-cluster OpenBao OR an external Vault address. This decouples "use the secret
store" from "render the in-cluster OpenBao server", so openbao.enabled=false + externalAddr consumes from
the external Vault WITHOUT falling back to plaintext secrets in pod env (the closed leak). */}}
{{- define "lance.secretsViaDapr" -}}
{{- if or .Values.openbao.enabled .Values.openbao.externalAddr -}}true{{- end -}}
{{- end -}}

{{/* Sidecar-only lineage routes: Dapr-delivered (pub/sub ingest + the cron reconcile binding), auth'd by
the app-api-token the sidecar stamps — they must NEVER be reachable through the public edge, which would
otherwise proxy them via the gateway's own sidecar (stamping that same trusted token). ONE source for the
gateway's 403 blocks — add any new Dapr-delivered lineage route here. Since the nginx gateway retired
(lance-ns-merge.md P1/P4) this renders a COMMA-separated list into the fleet ConfigMap's
RASK_LINEAGE_SIDECAR_ONLY_ROUTES, consumed by the FastAPI gateway's `lineage_sidecar_guard` middleware
(services/gateway). `lineage-events` matches the app's subscription route (lineage/api/dapr.py). The
reconcile binding is blocked even when reconcile is disabled (the route isn't mounted then — belt and
suspenders). On the kgateway/Envoy migration these become "no HTTPRoute declared" (allow-list by
construction) — the service-side token check is the load-bearing guard either way. */}}
{{- define "lance.lineageSidecarOnlyRoutes" -}}
{{- $bn := .Values.services.lineage.reconcile.bindingName -}}
{{- /* The binding name is interpolated raw into the comma-separated 403 env AND used as a Dapr Component
     metadata.name — validate the charset at the render so a `,` (which would add an empty entry) or a
     space fails loudly here instead of silently weakening the guard. Same [a-z0-9-] shape a k8s name
     needs anyway. */ -}}
{{- if and $bn (not (regexMatch "^[a-zA-Z0-9._-]+$" $bn)) -}}
{{- fail (printf "services.lineage.reconcile.bindingName %q must match ^[A-Za-z0-9._-]+$ — it is interpolated into the gateway 403 blocklist env and used as a Dapr Component name" $bn) -}}
{{- end -}}
{{- /* `with` skips a blank binding name — a trailing `,` would add an empty entry to the blocklist. */ -}}
lineage-events{{ with $bn }},{{ . }}{{ end }}
{{- end -}}

{{/* OTel SDK env for an app (call: include "lance.otelEnv" (list $root "<service.name>")). The apps run
under `opentelemetry-instrument` and export all three signals OTLP. OTel-first: the target is the Collector
(lance.otlpEndpoint), which adds GreptimeDB's headers — so the app stays backend-agnostic (plain OTLP, no
vendor headers). ONLY on the direct-to-GreptimeDB paths (no Collector) does the app carry the db-name header
on every signal + the trace-pipeline header on traces (metrics/logs must NOT carry it → separate
*_TRACES_HEADERS). The SDK appends /v1/{traces,metrics,logs} to the endpoint.

NOT the same as "rask.otelEnv": this one is Collector-first and Python-launcher-specific (the
OTEL_PYTHON_* knobs); rask's fleet keeps its own direct-to-GreptimeDB block. Both names coexist. */}}
{{- define "lance.otelEnv" -}}
{{- $root := index . 0 -}}
{{- $svc := index . 1 -}}
{{- $o := $root.Values.observability -}}
- { name: OTEL_SERVICE_NAME, value: {{ $svc | quote }} }
- { name: OTEL_EXPORTER_OTLP_ENDPOINT, value: "{{ include "lance.otlpEndpoint" $root }}" }
- { name: OTEL_EXPORTER_OTLP_PROTOCOL, value: "http/protobuf" }
{{- if not (include "lance.otelViaCollector" $root) }}
- { name: OTEL_EXPORTER_OTLP_HEADERS, value: "x-greptime-db-name={{ $o.dbName }}" }
- { name: OTEL_EXPORTER_OTLP_TRACES_HEADERS, value: "x-greptime-db-name={{ $o.dbName }},x-greptime-pipeline-name={{ $o.tracePipeline }}" }
{{- end }}
- { name: OTEL_TRACES_EXPORTER, value: "otlp" }
- { name: OTEL_METRICS_EXPORTER, value: "otlp" }
{{/* Logs go out via the OTel SDK (OTLP → the Collector → GreptimeDB `opentelemetry_logs`) — the "three
signals, one SDK, all OTLP" path. NO double-ingest: the app pods carry `lance.dev/logs=otlp`, and the
Collector's filelog receiver drops that label (a filter processor), so the file-tailed infra logs (no OTel
SDK) land in the SAME `opentelemetry_logs` table without duplicating the app logs. Tradeoff: an app crash
BEFORE the SDK inits is only in `kubectl logs`, not GreptimeDB — the Collector's filelog tail still catches
it (it's not labelled until the pod is up, so it's not dropped). */}}
- { name: OTEL_LOGS_EXPORTER, value: "otlp" }
{{/* Default metric export interval is 60s — too slow to observe in a demo/test. Push every 5s. */}}
- { name: OTEL_METRIC_EXPORT_INTERVAL, value: "5000" }
- { name: OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED, value: "true" }
{{/* Don't trace the k8s probe endpoints — they're hit every 10s and bury real request spans (otel
signals.md § Exclude noisy endpoints). Launcher-driven instrumentation → the env var is the only lever. */}}
- { name: OTEL_PYTHON_FASTAPI_EXCLUDED_URLS, value: "/livez,/readyz,/metrics" }
- { name: OTEL_RESOURCE_ATTRIBUTES, value: "service.namespace=lance-ns,deployment.environment.name={{ $o.environment | default "kind" }},service.version={{ $root.Chart.AppVersion }}" }
{{- end -}}


{{/* lineage-kit's HTTP transport (call: include "lance.lineageEmitEnv" $root).

TWO INDEPENDENT lineage paths exist and they are easy to confuse:

  1. Dapr pub/sub — what the medallion producer/movers, compaction and the catalog use TODAY
     (<APP>_LINEAGE_TOPIC → NATS → the lineage service's subscriber). Wired separately.
  2. lineage-kit's `LineageRun` / `@stage` / actor machinery — the DECORATABLE seam meant for the Ray
     Data pipeline (P7b). It resolves its transport through `LineageRun.emitter` → `default_emitter()`
     → `build_emitter()`, which reads RASK_LINEAGE_ENDPOINT.

Path 2's failure mode is silent by construction: with no endpoint, `build_emitter` logs ONE
`log.warning("lineage_http_without_endpoint …")` at startup and returns `NoopEmitter`, which then
drops every event at DEBUG. A pipeline emitting into a no-op is indistinguishable, from outside,
from a pipeline that never emitted — the graph is simply empty and everything reports success.

So the transport is rendered BEFORE anything emits through it, for the same reason the `parent` facet
is parsed before anything emits one: the arrival of the first real actor run must not be the moment
we discover the wire was never connected. Points at the lineage service's HTTP ingest
(`POST /api/v1/lineage` — services/lineage/api/v1/endpoints/ingest.py), which is the same graph the
Dapr subscriber writes to, so both paths converge.

endpoint_path is rendered explicitly rather than left to its in-code default: a drift between the two
would 404, and ClientEmitter catches-and-logs transport errors, so that too would fail silently. */}}
{{- define "lance.lineageEmitEnv" -}}
{{- $root := index . 0 -}}
- { name: RASK_LINEAGE_ENDPOINT, value: "http://{{ include "lance.fullname" $root }}-lineage:{{ $root.Values.services.lineage.port }}" }
- { name: RASK_LINEAGE_ENDPOINT_PATH, value: "api/v1/lineage" }
{{- /* NO service-door credentials are rendered here, deliberately. These pods reach lineage over the
     DAPR subscription route, which the sidecar-stamped app token already guards; the HTTP service door
     (LINEAGE_SERVICE_TOKEN + LINEAGE_SERVICE_ID) exists for the sidecar-LESS producers — the Ray train
     job (ray_submit.py passes both through the job's runtime_env) and the frontend zones' governed read
     (lance.frontendEnv). Both already carry them, and lineage-kit reads those exact names, so a producer
     that has them authenticates and one that does not stays on the open dev path. The allowlist those
     subjects are checked against is rendered once, on the lineage service (services.yaml). */}}
{{- end -}}


{{/* Rollout drain: a preStop sleep holds SIGTERM until endpoint removal has propagated to kube-proxy /
the Dapr sidecar, so in-flight requests drain instead of hitting connection-refused — this is what makes
the apps' /readyz shutting_down branch actually reachable during a rollout. Pairs with pod-level
terminationGracePeriodSeconds (grace > preStop + app drain). sh exists in every app image
(python-slim for the fleet, oven/bun's debian base for the zones). */}}
{{- define "lance.preStop" -}}
lifecycle:
  preStop:
    exec:
      command: ["sh", "-c", "sleep {{ .Values.lifecycle.preStopSeconds }}"]
{{- end -}}

{{/* HTTP health probes for the FastAPI app workloads (catalog/lineage/producer/movers/compaction). Two
distinct signals: readiness (/readyz) is dependency-aware (503 until the pool/namespace is up AND again once
draining) so k8s only routes traffic to a truly-ready pod; liveness (/livez) is process-up only (never
checks a backend — a slow dependency must NOT trigger a restart loop). Liveness runs slower + more tolerant
(failureThreshold 3 × 20s) so a busy-but-alive worker is never SIGKILLed. One helper = every app agrees. */}}
{{/* Soft anti-affinity: spread a component's replicas across NODES so a single node drain/failure can't
take the whole service down — otherwise the prod replicas:2 can co-locate and the PodDisruptionBudget buys
nothing (audit: "the HA replica count buys nothing"). ScheduleAnyway (NOT DoNotSchedule) so single-node
kind still schedules every replica. Gated by the caller on podDisruptionBudget.enabled (the prod HA signal
that also bumps replicas). Call: include "lance.spreadConstraints" "<component-label>". (prod-readiness P2) */}}
{{/* Per-workload resource tier: resources.<comp> if defined, else resources.default. Lets a stateful store
(age/rustfs) or the Arrow-IPC-buffering catalog be sized ABOVE the stateless-pod default without a
per-template edit — just set resources.<comp> in values(-prod). Every workload shared one 1-CPU/512Mi
default before, so the stores + the 256MiB-body catalog were sized like request pods (audit). Call:
include "lance.resources" (dict "root" $ "comp" "catalog"). (prod-readiness P2) */}}
{{- define "lance.resources" -}}
{{- $tier := (index .root.Values.resources .comp) | default .root.Values.resources.default -}}
{{- toYaml $tier -}}
{{- end -}}

{{- define "lance.spreadConstraints" -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        app.kubernetes.io/component: {{ . }}
{{- end -}}

{{- define "lance.appProbes" -}}
{{/* startupProbe gates liveness+readiness until boot completes: the FastAPI lifespan (Dapr secret fetch
     ~80s worst case + AGE pool + DDL + FGA provision) runs BEFORE uvicorn accepts connections, so nothing
     answers /livez during boot. Without this, liveness (armed ~70s in) SIGKILLs a still-initializing pod
     into CrashLoopBackOff exactly when a dependency is already slow. 30×10s = 300s boot budget, then the
     fast liveness/readiness cadence takes over. (prod-readiness P1) */}}
startupProbe:
  httpGet: { path: /livez, port: http }
  periodSeconds: 10
  failureThreshold: 30
readinessProbe:
  httpGet: { path: /readyz, port: http }
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  httpGet: { path: /livez, port: http }
  initialDelaySeconds: 10
  periodSeconds: 20
  timeoutSeconds: 3
  failureThreshold: 3
{{- end -}}

{{/* TCP health probes for non-HTTP-health workloads — the SvelteKit web pod (no /readyz route) and RustFS
(S3 API, no health route). A successful TCP accept on the serving port is the liveness/readiness signal.
Call: include "lance.tcpProbes" "<portName>" (the named container port to dial). */}}
{{- define "lance.tcpProbes" -}}
readinessProbe:
  tcpSocket: { port: {{ . }} }
  initialDelaySeconds: 5
  periodSeconds: 10
livenessProbe:
  tcpSocket: { port: {{ . }} }
  initialDelaySeconds: 10
  periodSeconds: 20
  failureThreshold: 3
{{- end -}}

{{/* Container hardening applied to every APP container (our images: catalog/lineage/web/movers/compaction).
runAsNonRoot enforces the image's non-root USER (catalog uid 10001, web `bun`) at admission — a manifest that
regressed to root fails to start instead of running privileged. drop ALL caps + no privilege escalation +
the RuntimeDefault seccomp profile = the restricted PodSecurity baseline. readOnlyRootFilesystem is on by
default (values.security.readOnlyRootFilesystem) — each app mounts an emptyDir at /tmp for scratch (pyarrow
spill, OTel), so nothing needs a writable rootfs. Container-level (NOT pod-level) so it never touches the
injected daprd sidecar or the busybox wait-age initContainer (which legitimately runs as root). */}}
{{- define "lance.securityContext" -}}
securityContext:
  runAsNonRoot: true
  allowPrivilegeEscalation: false
  {{/* UNCONDITIONAL. This used to be relaxed to false whenever `dev.reload` was set, which existed
       solely so Tilt's live_update could write into a running container. Tilt is gone (2026-08-04),
       and with it the only reason this chart could ever be told to unlock a container's filesystem.
       A chart a reconciler applies should not carry a values flag that weakens it. */}}
  readOnlyRootFilesystem: {{ .Values.security.readOnlyRootFilesystem }}
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{/* The writable-/tmp scratch pair that makes readOnlyRootFilesystem feasible: an emptyDir volume + its
mount. Emitted as a container volumeMount via "lance.tmpMount" and a pod volume via "lance.tmpVolume" so a
read-only rootfs still has a place for pyarrow/Lance spill + OTel. Only needed when readOnlyRootFilesystem
is on; harmless (an unused tmpfs) when off, so unconditionally included keeps the templates uniform. */}}
{{- define "lance.tmpMount" -}}
- { name: tmp, mountPath: /tmp }
{{- end -}}
{{- define "lance.tmpVolume" -}}
- { name: tmp, emptyDir: {} }
{{- end -}}

{{/*
A FULLY-RESOLVED image reference for a first-party component. Call:
  include "rask.image" (list $root "gateway")

GITOPS IS THE FIRST-CLASS CONSUMER of this chart, and that decides the two rules below.

1. `image.repository` is REQUIRED. It used to default to "", which rendered a BARE `gateway:dev` —
   and a bare name is not a local image, it is `docker.io/library/gateway:dev`. That only ever
   appeared to work because `make k3s-import` had side-loaded the tag into the node's containerd,
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
*/}}
{{- define "rask.image" -}}
{{- $root := index . 0 -}}{{- $name := index . 1 -}}
{{- /* Optional 3rd element: an explicit tag that beats image.tag — the per-zone `tag` on a
       frontend.apps entry, which is the only thing a zone boundary actually buys (independent
       deploy). A digest still wins over it, so a pinned reconciler is never overridden by a tag. */ -}}
{{- $override := "" -}}{{- if gt (len .) 2 -}}{{- $override = index . 2 -}}{{- end -}}
{{- $i := $root.Values.image -}}
{{- $digest := $i.digest | default "" -}}
{{- if $i.localImages -}}
{{- /* Side-loaded: a bare name the kubelet must already hold. Never valid for a remote cluster. */ -}}
{{- printf "%s:%s" $name (required "image.tag must be set" ($override | default $i.tag)) -}}
{{- else -}}
{{- $repo := required "image.repository must be set to a registry (e.g. ghcr.io/<org>/<repo>) — or set image.localImages=true if the images are side-loaded into the node (make k3s-import). A bare name resolves to Docker Hub and will ImagePullBackOff." $i.repository -}}
{{- if $digest -}}
{{- printf "%s/%s@%s" $repo $name $digest -}}
{{- else -}}
{{- printf "%s/%s:%s" $repo $name (required "image.tag must be set (a release tag in prod; `dev` locally), or set image.digest" ($override | default $i.tag)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
