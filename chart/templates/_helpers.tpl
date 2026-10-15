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

{{- define "rask.componentLabels" -}}
{{- $root := index . 0 -}}
{{- $component := index . 1 -}}
{{ include "rask.labels" $root }}
app.kubernetes.io/component: {{ $component }}
{{- end -}}

{{- define "rask.daprPodLabels" -}}
{{- if .Values.dapr.enabled -}}
dapr.io/enabled: "true"
{{- end }}
{{- end -}}

{{- define "rask.daprAnnotations" -}}
{{- $root := index . 0 -}}
{{- $appId := index . 1 -}}
{{- $appPort := index . 2 -}}
{{- if $root.Values.dapr.enabled -}}
dapr.io/enabled: "true"
dapr.io/app-id: {{ $appId | quote }}
dapr.io/app-port: {{ $appPort | quote }}
dapr.io/log-level: {{ $root.Values.dapr.logLevel | quote }}
dapr.io/log-as-json: "true"
{{- with $root.Values.dapr.maxBodySize }}
dapr.io/max-body-size: {{ . | quote }}
{{- end }}
dapr.io/app-token-secret: {{ $root.Release.Name }}-dapr-app-token
dapr.io/block-shutdown-duration: {{ printf "%ds" (int $root.Values.lifecycle.sidecarBlockShutdownSeconds) | quote }}
{{- include "lance.daprSidecarResources" $root | nindent 0 }}

{{- if $root.Values.dapr.enabled }}

{{- if has $appId (splitList "," (include "lance.secretScopes" $root)) }}
dapr.io/config: {{ include "lance.daprAppConfigName" (list $root $appId) | quote }}
{{- else }}
dapr.io/config: "lance-tracing"
{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "lance.daprOtlpTarget" -}}
{{- if .Values.observability.enabled -}}{{ include "lance.fullname" . }}-otel-collector:4318{{- end -}}
{{- end -}}

{{- define "rask.otelResourceAttrs" -}}
{{- $o := .Values.observability -}}
{{- printf "service.namespace=rask,deployment.environment.name=%s,service.version=%s" ($o.environment | default .Release.Namespace) .Chart.AppVersion -}}
{{- end -}}


{{- define "rask.zoneOtelEnv" -}}
{{- $root := index . 0 -}}
{{- $svc := index . 1 -}}
{{- $o := $root.Values.observability -}}
{{- if include "lance.otelEnabled" $root -}}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: "{{ include "lance.otlpEndpoint" $root }}"
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: "http/protobuf"
- name: OTEL_SERVICE_NAME
  value: {{ $svc | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ include "rask.otelResourceAttrs" $root | quote }}
{{- end }}
{{- end -}}

{{- define "rask.otelEnv" -}}
{{- $root := index . 0 -}}
{{- $svc := index . 1 -}}
{{- $o := $root.Values.observability -}}

{{- if include "lance.otelEnabled" $root -}}
{{/**/}}
- name: RASK_OTEL_ENABLED
  value: "true"

- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: "{{ include "lance.otlpEndpoint" $root }}"
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: "http/protobuf"

- name: OTEL_METRIC_EXPORT_INTERVAL
  value: "5000"

- name: OTEL_PYTHON_FASTAPI_EXCLUDED_URLS
  value: "/livez,/readyz,/healthz,/api/health,/metrics"
- name: OTEL_SERVICE_NAME
  value: {{ $svc | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ include "rask.otelResourceAttrs" $root | quote }}
{{- end }}
{{- end -}}

{{- define "lance.fullname" -}}{{ .Release.Name }}{{- end -}}

{{- define "rask.bootstrapRev" -}}r{{ .Release.Revision }}{{- end -}}

{{- define "rask.isRealDeployment" -}}
{{- $repo := (.Values.image.repository | default "") -}}
{{- $sideloaded := or .Values.image.localImages (hasPrefix "localhost:" $repo) (hasPrefix "127.0.0.1:" $repo) (hasPrefix "172.17.0.1:" $repo) -}}
{{- if or (not .Values.openbao.devMode) (not $sideloaded) -}}true{{- end -}}
{{- end -}}

{{- define "lance.labels" -}}
app.kubernetes.io/name: lance-ns
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "lance.ageHost" -}}{{ .Release.Name }}-age{{- end -}}

{{- define "lance.openfgaHost" -}}{{ .Release.Name }}-openfga{{- end -}}

{{- define "lance.dexHost" -}}{{ .Release.Name }}-dex{{- end -}}

{{- define "lance.minioHost" -}}{{ include "rask.fullname" . }}-minio{{- end -}}

{{- define "lance.openbaoHost" -}}{{ .Release.Name }}-openbao{{- end -}}

{{- define "lance.catalogImage" -}}
{{- $name := (.Values.image.catalog).repository | default "lance-rest-catalog" -}}
{{- include "rask.image" (list . $name) -}}
{{- end -}}

{{- define "lance.s3Endpoint" -}}
{{- if .Values.minio.externalEndpoint -}}{{ .Values.minio.externalEndpoint }}{{- else -}}http://{{ include "lance.minioHost" . }}:{{ .Values.minio.port }}{{- end -}}
{{- end -}}

{{- define "lance.ageConnectHost" -}}
{{- if .Values.age.externalHost -}}{{ .Values.age.externalHost }}
{{- else if .Values.age.enabled -}}{{ include "lance.ageHost" . }}
{{- else if .Values.age.cnpgCluster.enabled -}}lance-pg-rw
{{- else -}}{{ fail "age.enabled is false, so the Postgres behind AGE and OpenFGA is the platform's: set age.externalHost to its host (docs/architecture/deployment.md, Prerequisites)" }}
{{- end -}}
{{- end -}}

{{- define "lance.openfgaUrl" -}}
{{- if .Values.openfga.enabled -}}http://{{ include "lance.openfgaHost" . }}:8080
{{- else -}}{{ required "openfga.enabled is false, so OpenFGA is the platform's: set auth.fgaApiUrl to its HTTP API (docs/architecture/deployment.md, Prerequisites)" .Values.auth.fgaApiUrl }}
{{- end -}}
{{- end -}}

{{- define "lance.otlpEndpoint" -}}http://{{ include "lance.fullname" . }}-otel-collector:4318{{- end -}}

{{- define "lance.urlPort" -}}
{{- $u := urlParse . -}}
{{- $hp := splitList ":" $u.host -}}
{{- if gt (len $hp) 1 -}}{{ last $hp }}{{- else if eq $u.scheme "https" -}}443{{- else -}}80{{- end -}}
{{- end -}}

{{- define "lance.otelEnabled" -}}
{{- if .Values.observability.enabled -}}true{{- end -}}
{{- end -}}

{{- define "lance.openbaoServiceAccount" -}}
{{ include "lance.fullname" . }}-sa-openbao
{{- end -}}

{{- define "lance.vaultAddr" -}}
{{- if .Values.openbao.externalAddr -}}{{ .Values.openbao.externalAddr }}{{- else -}}http://{{ include "lance.openbaoHost" . }}:{{ .Values.openbao.port }}{{- end -}}
{{- end -}}

{{- define "lance.vaultAddrFQDN" -}}
{{- if .Values.openbao.externalAddr -}}{{ .Values.openbao.externalAddr }}{{- else -}}http://{{ include "lance.openbaoHost" . }}.{{ .Release.Namespace }}.svc.cluster.local:{{ .Values.openbao.port }}{{- end -}}
{{- end -}}

{{- define "lance.natsUrl" -}}
{{- required "NATS JetStream is a prerequisite: set nats.url to the bus every pub/sub component and the stream Job connect to (docs/architecture/deployment.md, Prerequisites); rask never installs it" .Values.nats.url -}}
{{- end -}}

{{- define "lance.subPubsub" -}}
{{- $root := index . 0 -}}
{{- $root.Values.pubsub.name }}-{{ index . 1 -}}
{{- end -}}

{{- define "lance.natsTable" -}}
{{- $t := deepCopy (.Values.nats.auth.users | default dict) -}}
{{- range $path, $grants := (.Values.nats.auth.flagged | default dict) -}}
{{- $v := $.Values -}}
{{- range $k := splitList "." $path }}{{- if kindIs "map" $v }}{{- $v = get $v $k }}{{- else }}{{- $v = false }}{{- end }}{{- end -}}
{{- if $v -}}
{{- range $u, $g := $grants -}}
{{- $row := get $t $u | default dict -}}
{{- $_ := set $t $u (dict "publish" (concat ($row.publish | default list) ($g.publish | default list) | uniq) "subscribe" (concat ($row.subscribe | default list) ($g.subscribe | default list) | uniq)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- toJson $t -}}
{{- end -}}

{{- define "lance.natsUsers" -}}
{{- $names := list -}}
{{- range $u, $p := (include "lance.natsTable" . | fromJson) -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9-]*$" $u) }}{{- fail (printf "nats.auth.users.%s: a user name must match ^[a-z0-9][a-z0-9-]*$" $u) }}{{- end -}}
{{- if or (not $p.publish) (not $p.subscribe) }}{{- fail (printf "nats.auth.users.%s: an empty publish or subscribe list is issued by nsc as unrestricted" $u) }}{{- end -}}
{{- range (concat ($p.publish | default list) ($p.subscribe | default list)) }}{{- if not (regexMatch "^[A-Za-z0-9_$.*>-]+$" .) }}{{- fail (printf "nats.auth.users.%s: %q is not a NATS subject this chart can pass to nsc" $u .) }}{{- end }}{{- end -}}
{{- $names = append $names $u -}}
{{- end -}}
{{- join " " $names -}}
{{- end -}}

{{- define "lance.natsCreds" -}}
{{- $root := index . 0 -}}{{- $app := index . 1 -}}
{{- if include "lance.secretsViaDapr" $root -}}
{{- if not (has $app (include "lance.natsUsers" $root | splitList " ")) }}{{- fail (printf "a pub/sub component of %s names nats-user-%s, which nats.auth.users does not declare" $app $app) }}{{- end -}}
- { name: jwt, secretKeyRef: { name: nats-user-{{ $app }}, key: jwt } }
- { name: seedKey, secretKeyRef: { name: nats-user-{{ $app }}, key: seed } }
{{- end -}}
{{- end -}}

{{- define "lance.natsAuth" -}}
{{- if include "lance.secretsViaDapr" . }}
auth: { secretStore: lance-secrets }
{{- end }}
{{- end -}}

{{- define "lance.publishPubsub" -}}
{{- $root := index . 0 -}}{{- $name := index . 1 -}}{{- $app := index . 2 }}
---
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: {{ $name }}
  labels: {{- include "lance.labels" $root | nindent 4 }}
spec:
  type: pubsub.jetstream
  version: v1
  metadata:
    - { name: natsURL, value: "{{ include "lance.natsUrl" $root }}" }
    - { name: name, value: "lance-dapr-{{ $name }}" }
    {{- with include "lance.natsCreds" (list $root $app) }}{{ . | nindent 4 }}{{- end }}
{{- include "lance.natsAuth" $root }}
scopes:
  - {{ $app }}
{{- end -}}

{{- define "lance.mintsDevCredentials" -}}
{{- if and .Values.openbao.enabled .Values.openbao.devMode (not .Values.openbao.externalAddr) -}}true{{- end -}}
{{- end -}}

{{- define "lance.natsMints" -}}
{{- include "lance.mintsDevCredentials" . -}}
{{- end -}}

{{- define "lance.natsCredsFiles" -}}
{{- if include "lance.secretsViaDapr" . -}}true{{- end -}}
{{- end -}}

{{- define "lance.natsFiles" -}}
{{- $files := dict -}}
{{- if include "lance.natsCredsFiles" . -}}
{{- $_ := set $files "nats-admin-creds" (list "nats-user-admin") -}}
{{- if include "lance.natsMints" . }}
{{- $_ := set $files "nats-box-creds" (list "nats-user-monitor") }}
{{- $_ := set $files "nats-auth" (list "nats-server" "nats-route") }}
{{- end -}}
{{- end -}}
{{- toJson $files -}}
{{- end -}}

{{- define "lance.daprSidecarResources" -}}
{{- $r := .Values.dapr.sidecarResources -}}
dapr.io/sidecar-cpu-request: {{ $r.cpuRequest | quote }}
dapr.io/sidecar-cpu-limit: {{ $r.cpuLimit | quote }}
dapr.io/sidecar-memory-request: {{ $r.memoryRequest | quote }}
dapr.io/sidecar-memory-limit: {{ $r.memoryLimit | quote }}

dapr.io/disable-builtin-k8s-secret-store: "true"
{{- if .Values.dapr.sidecarRestricted }}

dapr.io/sidecar-seccomp-profile-type: RuntimeDefault
{{- end }}
{{- end -}}

{{- define "lance.secretsViaDapr" -}}
{{- if or .Values.openbao.enabled .Values.openbao.externalAddr -}}true{{- end -}}
{{- end -}}

{{- define "lance.lineageSidecarOnlyRoutes" -}}
{{- $bn := .Values.services.lineage.reconcile.bindingName -}}

{{- if and $bn (not (regexMatch "^[a-zA-Z0-9._-]+$" $bn)) -}}
{{- fail (printf "services.lineage.reconcile.bindingName %q must match ^[A-Za-z0-9._-]+$ — it is interpolated into the gateway 403 blocklist env and used as a Dapr Component name" $bn) -}}
{{- end -}}

lineage-events{{ with $bn }},{{ . }}{{ end }}
{{- end -}}

{{- define "lance.otelEnv" -}}
{{- $root := index . 0 -}}
{{- $svc := index . 1 -}}
{{- $o := $root.Values.observability -}}
- { name: OTEL_SERVICE_NAME, value: {{ $svc | quote }} }
- { name: OTEL_EXPORTER_OTLP_ENDPOINT, value: "{{ include "lance.otlpEndpoint" $root }}" }
- { name: OTEL_EXPORTER_OTLP_PROTOCOL, value: "http/protobuf" }
- { name: OTEL_TRACES_EXPORTER, value: "otlp" }
- { name: OTEL_METRICS_EXPORTER, value: "otlp" }

- { name: OTEL_LOGS_EXPORTER, value: "otlp" }

- { name: OTEL_METRIC_EXPORT_INTERVAL, value: "5000" }
- { name: OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED, value: "true" }
{{/**/}}
- { name: OTEL_PYTHON_FASTAPI_EXCLUDED_URLS, value: "/livez,/readyz,/healthz,/api/health,/metrics" }
- { name: OTEL_RESOURCE_ATTRIBUTES, value: "{{ include "rask.otelResourceAttrs" $root }}" }
{{- end -}}

{{- define "lance.lineageEmitEnv" -}}
{{- $root := index . 0 -}}
- { name: RASK_LINEAGE_ENDPOINT, value: "http://{{ include "lance.fullname" $root }}-lineage:{{ $root.Values.services.lineage.port }}" }
- { name: RASK_LINEAGE_ENDPOINT_PATH, value: "api/v1/lineage" }

{{- end -}}

{{- define "lance.preStop" -}}
lifecycle:
  preStop:
    exec:
      command: ["sh", "-c", "sleep {{ .Values.lifecycle.preStopSeconds }}"]
{{- end -}}

{{- define "rask.shutdownArg" -}}
{{- $r := . -}}{{- $g := 0 -}}
{{- if kindIs "slice" . }}{{ $r = index . 0 }}{{ $g = index . 1 }}{{ else }}{{ $g = .Values.lifecycle.terminationGracePeriodSeconds }}{{ end -}}
{{- $l := $r.Values.lifecycle -}}
{{- $t := sub $g (add $l.preStopSeconds (required "lifecycle.teardownSeconds" $l.teardownSeconds)) -}}
{{- if or (le $t 0) (lt (int $l.teardownSeconds) 0) }}{{ fail "lifecycle: need 0 <= teardownSeconds < grace - preStopSeconds" }}{{ end -}}
- "--timeout-graceful-shutdown={{ $t }}"
{{- end -}}

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

{{- define "rask.proxyHeaderArgs" -}}
{{- $cidr := .Values.forwardedAllowIps | default "127.0.0.1" -}}
{{- if eq (toString $cidr) "*" -}}
{{- fail "forwardedAllowIps must not be '*': it trusts every peer, so any client can forge its own source IP via x-forwarded-for. Set it to the ingress controller's pod CIDR." -}}
{{- end -}}
- "--proxy-headers"
- "--forwarded-allow-ips={{ $cidr }}"
{{- end -}}

{{- define "rask.fleetProbes" -}}
{{- $svc := . -}}
{{- $live := ($svc.healthPath | default "/livez") -}}
startupProbe:
  httpGet: { path: {{ $live }}, port: http }
  periodSeconds: 10
  failureThreshold: 30
  timeoutSeconds: 5
readinessProbe:
  httpGet: { path: {{ $svc.readyPath | default "/readyz" }}, port: http }
  initialDelaySeconds: 5
  periodSeconds: 10
  timeoutSeconds: 5
livenessProbe:
  httpGet: { path: {{ $live }}, port: http }
  initialDelaySeconds: 15
  periodSeconds: 20
  timeoutSeconds: 5
{{- end -}}

{{- define "lance.appProbes" -}}
{{/**/}}
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

{{- define "lance.rootSecurityContext" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "lance.rootlessSecurityContext" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: {{ . }}
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "lance.securityContext" -}}
securityContext:
  runAsNonRoot: true
  allowPrivilegeEscalation: false
  {{/**/}}
  readOnlyRootFilesystem: {{ .Values.security.readOnlyRootFilesystem }}
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end -}}

{{- define "lance.tmpMount" -}}
- { name: tmp, mountPath: /tmp }
{{- end -}}

{{- define "lance.tmpVolume" -}}
- { name: tmp, emptyDir: { sizeLimit: {{ .Values.security.tmpSizeLimit | default "2Gi" | quote }} } }
{{- end -}}

{{- define "rask.image" -}}
{{- $root := index . 0 -}}{{- $name := index . 1 -}}

{{- $override := "" -}}{{- if gt (len .) 2 -}}{{- $override = index . 2 -}}{{- end -}}
{{- $i := $root.Values.image -}}

{{- $perDigest := "" -}}{{- if $i.digests -}}{{- $perDigest = index $i.digests $name | default "" -}}{{- end -}}
{{- $perTag := "" -}}{{- if $i.tags -}}{{- $perTag = index $i.tags $name | default "" -}}{{- end -}}
{{- $digest := $perDigest | default ($i.digest | default "") -}}
{{- $override = $perTag | default $override -}}
{{- if $i.localImages -}}

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

{{- define "lance.governedAuthAck" -}}
- { name: RASK_INSECURE_ALLOW_UNAUTHENTICATED, value: "true" }
{{- end -}}

{{- define "lance.identityTokens" -}}
{{- $part := first . -}}
{{- range rest . }}
{{- if eq $part "volumes" }}
- name: identity-{{ . }}
  projected:
    sources:
      - serviceAccountToken: { path: token, audience: {{ . }}, expirationSeconds: 600 }
{{- else }}
- { name: identity-{{ . }}, mountPath: /var/run/secrets/rask/identity/{{ . }}, readOnly: true }
{{- end }}
{{- end }}
{{- end -}}

{{- define "lance.fleetAudiences" -}}
{{- join " " ((index . 1).identityAudiences | default list) -}}
{{- end -}}

{{- define "lance.saSubject" -}}
{{- $root := index . 0 -}}
{{- printf "kubernetes~system:serviceaccount:%s:%s-sa-%s" $root.Release.Namespace (include "lance.fullname" $root) (index . 1) -}}
{{- end -}}

{{- define "lance.saVerifier" -}}
{{- $root := index . 0 -}}{{- $part := index . 1 -}}{{- $door := index . 2 -}}
{{- if $root.Values.auth.enabled -}}
{{- if eq $part "env" }}
- { name: RASK_SA_ISSUER, value: {{ $root.Values.auth.serviceAccountIssuer | quote }} }
- { name: RASK_SA_AUDIENCE, value: {{ printf "rask-%s" $door }} }
- { name: RASK_SA_FETCH_TOKEN_FILE, value: /var/run/secrets/rask/sa-fetch/token }
- { name: RASK_SA_CA_FILE, value: /var/run/secrets/rask/sa-fetch/ca.crt }
{{- else }}
{{- include "lance.saFetch" $part }}
{{- end }}
{{- end -}}
{{- end -}}

{{- define "lance.saFetch" -}}
{{- if eq . "volumes" }}
- name: sa-fetch
  projected:
    sources:
      - serviceAccountToken: { path: token, expirationSeconds: 3600 }
      - configMap: { name: kube-root-ca.crt, items: [{ key: ca.crt, path: ca.crt }] }
{{- else }}
- { name: sa-fetch, mountPath: /var/run/secrets/rask/sa-fetch, readOnly: true }
{{- end }}
{{- end -}}

{{- define "lance.governedOidcEnv" -}}
{{- $root := . }}
- { name: RASK_OIDC_ENABLED, value: "true" }
- { name: RASK_OIDC_ISSUER, value: {{ $root.Values.dex.issuer | quote }} }
- { name: RASK_OIDC_DISCOVERY_URL, value: "http://{{ include "lance.dexHost" $root }}:{{ $root.Values.dex.port }}/dex" }
- { name: RASK_OIDC_AUDIENCE, value: {{ $root.Values.dex.clientId | quote }} }
- { name: RASK_OIDC_ALLOW_INSECURE, value: {{ ternary "false" "true" (hasPrefix "https://" $root.Values.dex.issuer) | quote }} }
{{- end -}}

{{- define "lance.governedFgaEnv" -}}
{{- $root := . }}
- { name: RASK_FGA_ENABLED, value: "true" }
- { name: RASK_FGA_API_URL, value: {{ include "lance.openfgaUrl" $root | quote }} }
{{- include "lance.fgaToken" (list "env") }}
{{- end -}}

{{- define "lance.openfgaAudience" -}}rask-openfga{{- end -}}

{{- define "lance.openfgaSubjects" -}}
{{- $v := .Values -}}{{- $p := printf "system:serviceaccount:%s:" .Release.Namespace -}}{{- $sa := printf "%s%s-sa-" $p (include "lance.fullname" .) -}}
{{- $out := list -}}
{{- if $v.auth.enabled -}}
{{- range list "catalog" "lineage" "jobs" "hooks" }}{{ $out = append $out (printf "%s%s" $sa .) }}{{ end -}}
{{- if $v.maintenance.enabled }}{{ $out = append $out (printf "%smaintenance" $sa) }}{{ end -}}
{{- if $v.explorer.enabled }}{{ range $n, $_ := $v.explorer.services }}{{ $out = append $out (printf "%s%s" $sa $n) }}{{ end }}{{ end -}}
{{- range $n, $s := $v.services }}{{ if and (not (has $n (list "catalog" "lineage"))) (or $s.frontDoor $v.singleTenant.enabled) $s.governedAuth }}{{ $out = append $out (printf "%s%s" $sa $n) }}{{ end }}{{ end -}}
{{- range ($v.auth.externalFgaClients | default list) }}{{- if not (regexMatch "^[a-z0-9-]+:[a-z0-9.-]+$" .) }}{{- fail (printf "auth.externalFgaClients entry %q is not <namespace>:<serviceaccount>" .) }}{{- end }}{{ $out = append $out (printf "system:serviceaccount:%s" .) }}{{ end -}}
{{- end -}}
{{- $out | uniq | sortAlpha | join "," -}}
{{- end -}}

{{- define "lance.fgaToken" -}}
{{- $aud := include "lance.openfgaAudience" . -}}
{{- if eq (first .) "env" }}
- { name: RASK_FGA_TOKEN_FILE, value: /var/run/secrets/rask/identity/{{ $aud }}/token }
{{- else }}
{{- include "lance.identityTokens" (list (first .) $aud) }}
{{- end }}
{{- end -}}

{{- define "lance.governedFgaPins" -}}
{{- $root := . }}
{{- with $root.Values.auth.fgaStoreId }}
- { name: RASK_FGA_STORE_ID, value: {{ . | quote }} }
{{- end }}
{{- with $root.Values.auth.fgaModelId }}
- { name: RASK_FGA_MODEL_ID, value: {{ . | quote }} }
{{- end }}
{{- end -}}

{{- define "lance.corpusVolume" -}}
{{- $corpus := .Values.explorer.corpus | default dict -}}
{{- $mode := $corpus.mode | default "emptyDir" -}}
{{- if not (has $mode (list "emptyDir" "pvc" "hostPath")) -}}
{{- fail (printf "rask chart: explorer.corpus.mode=%q is not one of emptyDir|pvc|hostPath" $mode) -}}
{{- end -}}
- name: corpus
{{- if eq $mode "pvc" }}
  persistentVolumeClaim:
    claimName: {{ $corpus.claimName | default (printf "%s-media-corpus" (include "lance.fullname" .)) | quote }}
{{- else if eq $mode "hostPath" }}
  hostPath: { path: {{ $corpus.hostPath | default "/var/media-corpus" | quote }}, type: DirectoryOrCreate }
{{- else }}
  emptyDir: {}
{{- end }}
{{- end -}}

{{- define "lance.bootstrapJobDeadline" -}}
activeDeadlineSeconds: 1800
{{- end -}}

{{- define "lance.catalogSecretField" -}}
{{- ternary "catalog-s3-secret-key" "minio-secret-key" (not (not .Values.minio.catalogAccessKey)) -}}
{{- end -}}

{{- define "lance.baseFixture" -}}
{{- $out := dict -}}
{{- if .Values.catalog.multibase.devSeed -}}
{{- if or (include "rask.isRealDeployment" .) (not .Values.minio.enabled) (not .Values.openbao.enabled) -}}
{{- fail "catalog.multibase.devSeed is a local fixture: it needs minio.enabled, the bundled dev-mode openbao and side-loaded images" -}}
{{- end -}}
{{- range $base, $ref := .Values.catalog.multibase.baseCredentialRefs | default dict -}}
{{- if not (and (hasPrefix "s3://" $base) (regexMatch "^[a-z0-9][a-z0-9-]*$" $ref)) -}}
{{- fail (printf "catalog.multibase.devSeed cannot seed %q=%q: it needs an s3:// base and a lowercase [a-z0-9-] reference" $base $ref) -}}
{{- end -}}
{{- $parts := splitn "/" 2 (trimPrefix "s3://" $base) -}}
{{- $loc := dict "bucket" $parts._0 "prefix" (trimSuffix "/" ($parts._1 | default "")) -}}
{{- $_ := set $out $ref (append (get $out $ref | default list) $loc) -}}
{{- end -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- define "lance.scopedSecretFn" -}}
scoped_secret() {
  s=$(printf '%s-s3-%s' "$1" "$root" | sha256sum | cut -c1-40)
  [ ${#s} -eq 40 ] || { echo "could not derive the scoped secret for $1" >&2; exit 1; }
  (umask 077; printf '%s' "$s" > "$2")
}
{{- end -}}

{{- define "lance.secretScopes" -}}
{{- $scopes := list .Values.services.catalog.daprAppId .Values.services.lineage.daprAppId -}}
{{- if .Values.maintenance.enabled -}}
{{- $scopes = append $scopes .Values.maintenance.daprAppId -}}
{{- end -}}
{{- if .Values.explorer.enabled -}}

{{- range $name, $svc := .Values.explorer.services -}}
{{- $scopes = append $scopes ($svc.daprAppId | default $name) -}}
{{- end -}}
{{- end -}}
{{- if .Values.stateStore.enabled -}}

{{- $scopes = concat $scopes .Values.stateStore.scopes -}}
{{- end -}}
{{- join "," ($scopes | uniq) -}}
{{- end -}}

{{- define "lance.appTokenEnv" -}}
{{- $root := index . 0 -}}{{- $appId := index . 1 -}}
{{- if and (include "lance.secretsViaDapr" $root) (has $appId (splitList "," (include "lance.secretScopes" $root))) -}}
- { name: RASK_APP_TOKEN_FROM_STORE, value: "true" }
{{- else -}}
- name: APP_API_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $root.Release.Name }}-dapr-app-token, key: token }
{{- end -}}
{{- end -}}

{{- define "lance.allocatorEnv" -}}
{{- $root := index . 0 -}}
- { name: MALLOC_ARENA_MAX, value: {{ $root.Values.allocator.arenaMax | quote }} }
- { name: ARROW_DEFAULT_MEMORY_POOL, value: {{ $root.Values.allocator.arrowMemoryPool | quote }} }
{{- end -}}

{{- define "lance.daprConfigSpecBody" -}}
  {{- /**/}}
  features:
    - name: HotReload
      enabled: false
{{- if include "lance.otelEnabled" . }}
  tracing:
    samplingRate: {{ .Values.observability.samplingRate | quote }}
{{- with include "lance.daprOtlpTarget" . }}
    otel:
      endpointAddress: {{ . | quote }}
      isSecure: false
      protocol: http
      timeout: "30s"
{{- end }}
{{- end }}
  metrics:
    enabled: true
    recordErrorCodes: true
    http:
      increasedCardinality: false
      pathMatching:
        - "/v1.0/state/{storeName}"
        - "/v1.0/publish/{pubsubName}/{topic...}"
        - "/v1.0/actors/{actorType}/{actorId}/method/{method}"
  logging:
    apiLogging:
      enabled: true
      obfuscateURLs: true
      omitHealthChecks: true
{{- with .Values.dapr.workflowRetention }}
{{- if .enabled }}
  workflow:

    {{- range $k, $v := (dict "completed" .completed "failed" .failed "terminated" .terminated) }}
    {{- if not (regexMatch "^([1-9][0-9]*)(h|m)$" (toString $v)) }}
    {{- fail (printf "dapr.workflowRetention.%s = %q — must be a positive duration in h or m. A zero or malformed value silently purges workflow history the instant an instance terminates." $k $v) }}
    {{- end }}
    {{- if and (hasSuffix "m" (toString $v)) (lt (int (trimSuffix "m" (toString $v))) 60) }}
    {{- fail (printf "dapr.workflowRetention.%s = %q is under an hour. Workflow history is the only record of what a run did; raise the floor here deliberately if that is genuinely wanted." $k $v) }}
    {{- end }}
    {{- end }}
    stateRetentionPolicy:
      completed: {{ .completed | quote }}
      failed: {{ .failed | quote }}
      terminated: {{ .terminated | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "lance.daprAppSpec" -}}
{{- $cfgRoot := index . 0 -}}{{- $app := index . 1 -}}
{{- include "lance.daprConfigSpecBody" $cfgRoot }}
  secrets:
    scopes:
      - storeName: lance-secrets
        defaultAccess: allow

        {{- $denied := list }}
        {{- $signs := include "lance.signingKeys" (list $cfgRoot $app) | splitList " " }}
        {{- range (include "lance.signingKeys" (list $cfgRoot) | splitList " ") }}
        {{- if and . (not (has . $signs)) }}
        {{- $denied = append $denied (printf "signing-key-%s" .) }}
        {{- end }}
        {{- end }}

        {{- range (include "lance.natsUsers" $cfgRoot | splitList " " | compact) }}
        {{- $denied = append $denied (printf "nats-user-%s" .) }}
        {{- end }}
        {{- $denied = concat $denied (list "nats-root" "nats-route") }}
        {{- if $denied }}
        deniedSecrets:
          {{- range $denied }}
          - {{ . }}
          {{- end }}
        {{- end }}
{{- end -}}

{{- define "lance.daprAppConfigName" -}}
{{- printf "lance-config-%s-%s" (index . 1) (include "lance.daprAppSpec" . | sha256sum | trunc 10) -}}
{{- end -}}

{{- define "lance.signers" -}}
{{- $v := .Values -}}{{- $s := list (dict "id" (include "lance.saSubject" (list . "catalog")) "key" (printf "%s-sa-catalog" (include "lance.fullname" .)) "app" $v.services.catalog.daprAppId "role" "catalog") -}}
{{- if $v.maintenance.enabled }}{{- $s = append $s (dict "id" (include "lance.saSubject" (list . "maintenance")) "key" (printf "%s-sa-maintenance" (include "lance.fullname" .)) "app" $v.maintenance.daprAppId "role" "maintenance") }}{{- end -}}
{{- toJson $s -}}
{{- end -}}

{{- define "lance.signingIdentities" -}}
{{- $ids := list -}}
{{- range (include "lance.signers" . | fromJsonArray) }}{{- $ids = append $ids .id }}{{- end -}}
{{- join " " ($ids | uniq | sortAlpha) -}}
{{- end -}}

{{- define "lance.signingKeys" -}}
{{- $keys := list -}}
{{- range (include "lance.signers" (first .) | fromJsonArray) }}
{{- if or (eq (len $) 1) (eq .app (index $ 1)) }}
{{- if not (regexMatch "^[a-z0-9][a-z0-9-]*$" .key) }}{{- fail (printf "signing key name %q must match ^[a-z0-9][a-z0-9-]*$: it becomes a shell word, an OpenBao path and a Dapr URL segment" .key) }}{{- end }}
{{- $keys = append $keys .key }}
{{- end }}
{{- end -}}
{{- join " " ($keys | uniq | sortAlpha) -}}
{{- end -}}

{{- define "lance.delegatorIdentities" -}}{{- include "lance.saSubject" (list . "catalog") -}}{{- end -}}

{{- define "lance.signingIdentitiesForApp" -}}
{{- $app := index . 1 -}}{{- $mine := list -}}
{{- range (include "lance.signers" (index . 0) | fromJsonArray) }}{{- if eq .app $app }}{{- $mine = append $mine .id }}{{- end }}{{- end -}}
{{- join " " ($mine | uniq | sortAlpha) -}}
{{- end -}}

{{- define "lance.signingEnv" -}}
{{- if include "lance.secretsViaDapr" (index . 0) }}
{{- range (include "lance.signingIdentitiesForApp" . | splitList " " | compact) }}- { name: RASK_SIGNING_IDENTITY, value: {{ . | quote }} }{{- end }}
{{- end }}
{{- end -}}

{{- define "lance.controlSignerRoles" -}}
{{- $m := dict "catalog" (list) "maintenance" (list) -}}
{{- range (include "lance.signers" . | fromJsonArray) }}{{- if .role }}{{- $_ := set $m .role (append (get $m .role) .id | uniq | sortAlpha) }}{{- end }}{{- end -}}
{{- toJson $m -}}
{{- end -}}

{{- define "lance.doorEnv" -}}
{{- $r := index . 0 -}}{{- $mode := ($r.Values.signing).doors -}}
{{- if not (has $mode (list "off" "observe" "enforce")) }}{{- fail (printf "signing.doors is %v, not the string off, observe or enforce" $mode) }}{{- end }}
{{- if and $r.Values.auth.enabled (include "lance.secretsViaDapr" $r) }}
{{- $signers := include "lance.signingIdentities" $r | splitList " " | compact }}
{{- if and (ne $mode "off") (not $signers) }}{{- fail (printf "signing.doors=%s but no identity signs: the doors would verify nothing" $mode) }}{{- end }}
{{- if gt (len .) 2 }}{{- $roles := include "lance.controlSignerRoles" $r | fromJson }}{{- $signers = list }}{{- range (index . 2) }}{{- $signers = concat $signers (get $roles .) }}{{- end }}{{- $signers = $signers | uniq | sortAlpha }}{{- end }}
- { name: RASK_SIGNATURE_DOORS, value: {{ $mode | quote }} }
- { name: RASK_EVENT_SIGNERS, value: {{ $signers | toJson | quote }} }
- { name: RASK_EVENT_DELEGATORS, value: {{ include "lance.delegatorIdentities" $r | splitList " " | compact | toJson | quote }} }
{{- if index . 1 }}
- { name: RASK_CONTROL_SIGNER_ROLES, value: {{ include "lance.controlSignerRoles" $r | quote }} }
{{- end }}
{{- end }}
{{- end -}}

{{- define "lance.signingShapes" -}}
B32=ABCDEFGHIJKLMNOPQRSTUVWXYZ234567
nkey_shaped() { case "$1" in "$2"*) ;; *) return 1 ;; esac; case "$1" in *[!$B32]*) return 1 ;; esac; [ "${#1}" -eq "$3" ]; }
seed_shaped() { nkey_shaped "$1" SU 58; }
pub_shaped() { nkey_shaped "$1" U 56; }
keys_shaped() { case "$1" in *,) return 1 ;; *,*) pub_shaped "${1%%,*}" && pub_shaped "${1#*,}" ;; *) pub_shaped "$1" ;; esac; }
{{- end -}}

{{- define "lance.platformBuckets" -}}
{{- $buckets := .Values.minio.buckets | default list -}}
{{- join "," (compact $buckets | uniq) -}}
{{- end -}}

{{- define "lance.maintenancePlatformBuckets" -}}
{{- $buckets := splitList "," (include "lance.platformBuckets" .) -}}
{{- range ((.Values.catalog).multibase).dataBases | default list -}}
{{- $buckets = append $buckets (first (splitList "/" (trimPrefix "s3://" .))) -}}
{{- end -}}
{{- join "," (compact $buckets | uniq) -}}
{{- end -}}

{{- define "lance.externalBlobBases" -}}
{{ .Values.vending.externalBlobBases | default (printf "s3://%s/models/" .Values.minio.bucket) }}
{{- end }}

{{- define "rask.rayDashboardUrl" -}}
{{- if .Values.ray.dashboardUrl -}}
{{- .Values.ray.dashboardUrl -}}
{{- end -}}
{{- end -}}

{{- define "rask.rayServeOrigin" -}}
{{- if .Values.frontend.serveUrl -}}
{{- .Values.frontend.serveUrl -}}
{{- else -}}
{{- include "rask.rayDashboardUrl" . -}}
{{- end -}}
{{- end -}}
