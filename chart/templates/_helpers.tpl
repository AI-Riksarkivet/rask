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
{{- include "lance.storeIdentity" (list $root $appId "annotation") }}
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
{{- if or (not .Values.openbao.enabled) (not $sideloaded) -}}true{{- end -}}
{{- end -}}

{{- define "lance.labels" -}}
app.kubernetes.io/name: lance-ns
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "lance.ageHost" -}}{{ .Release.Name }}-age{{- end -}}

{{- define "lance.openfgaHost" -}}{{ .Release.Name }}-openfga{{- end -}}

{{- define "lance.dexHost" -}}{{ .Release.Name }}-dex{{- end -}}

{{- define "lance.objectStoreHost" -}}{{ include "rask.fullname" . }}-rustfs{{- end -}}

{{- define "lance.openbaoHost" -}}{{ .Release.Name }}-openbao{{- end -}}

{{- define "lance.catalogImage" -}}
{{- $name := (.Values.image.catalog).repository | default "lance-rest-catalog" -}}
{{- include "rask.image" (list . $name) -}}
{{- end -}}

{{- define "lance.s3Endpoint" -}}
{{- if .Values.objectStore.externalEndpoint -}}{{ .Values.objectStore.externalEndpoint }}
{{- else if .Values.objectStore.enabled -}}http://{{ include "lance.objectStoreHost" . }}:{{ .Values.objectStore.port }}
{{- else -}}{{ fail "objectStore.enabled is false, so the S3 behind the lakehouse is the platform's: set objectStore.externalEndpoint to it (docs/architecture/deployment.md, Prerequisites)" }}
{{- end -}}
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

{{- define "lance.egressRules" -}}
{{- $value := index . 0 -}}{{- $rules := index . 1 -}}{{- $reaches := index . 2 -}}
{{- if not $rules }}
{{- fail (printf "%s must state the NetworkPolicy egress rules ({to, ports}) that reach %s" $value $reaches) }}
{{- end }}
{{- range $rules }}
{{- if not (.to | default list) }}
{{- fail (printf "%s: the rule %s names no `to` peers, so it admits every destination on its ports; name the peers that are %s" $value (toJson .) $reaches) }}
{{- end }}
{{- range .to }}
{{- if hasSuffix "/0" (toString ((.ipBlock | default dict).cidr | default "")) }}
{{- fail (printf "%s: the peer %s admits every destination; name the addresses of %s" $value (toJson .) $reaches) }}
{{- end }}
{{- end }}
{{- end }}
{{- toYaml $rules }}
{{- end -}}

{{- define "lance.otelEnabled" -}}
{{- if .Values.observability.enabled -}}true{{- end -}}
{{- end -}}

{{- define "lance.vaultAddr" -}}
{{- if .Values.openbao.enabled -}}http://{{ include "lance.openbaoHost" . }}:{{ .Values.openbao.port }}
{{- else -}}{{ include "lance.platformStoreAddr" . }}
{{- end -}}
{{- end -}}

{{- define "lance.vaultAddrFQDN" -}}
{{- if .Values.openbao.enabled -}}http://{{ include "lance.openbaoHost" . }}.{{ .Release.Namespace }}.svc.cluster.local:{{ .Values.openbao.port }}
{{- else -}}{{ include "lance.platformStoreAddr" . }}
{{- end -}}
{{- end -}}

{{- define "lance.platformStoreAddr" -}}
{{- $addr := required "openbao.enabled is false, so the secret store is the platform's: set openbao.externalAddr to its Vault or OpenBao (https; docs/architecture/deployment.md, Prerequisites)" .Values.openbao.externalAddr -}}
{{- if not (hasPrefix "https://" $addr) -}}
{{- fail (printf "openbao.externalAddr is %q: every sidecar sends the store's token to it, so the platform's secret store is reached over https only" $addr) -}}
{{- end -}}
{{- $addr -}}
{{- end -}}

{{- define "lance.natsUrl" -}}
{{- required "NATS JetStream is a prerequisite: set nats.url to the bus every pub/sub component and the stream Job connect to (docs/architecture/deployment.md, Prerequisites); rask never installs it" .Values.nats.url -}}
{{- end -}}

{{- define "lance.busEnv" -}}
- { name: RASK_BUS_URL, value: {{ include "lance.natsUrl" . | quote }} }
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
{{- if not (has $app (include "lance.natsUsers" $root | splitList " ")) }}{{- fail (printf "a pub/sub component of %s names nats-user-%s, which nats.auth.users does not declare" $app $app) }}{{- end -}}
- { name: jwt, secretKeyRef: { name: nats-user-{{ $app }}, key: jwt } }
- { name: seedKey, secretKeyRef: { name: nats-user-{{ $app }}, key: seed } }
{{- end -}}

{{- define "lance.natsAuth" }}
auth: { secretStore: lance-secrets }
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
{{- if .Values.openbao.enabled -}}true{{- end -}}
{{- end -}}

{{- define "lance.natsMints" -}}
{{- include "lance.mintsDevCredentials" . -}}
{{- end -}}

{{- define "lance.natsFiles" -}}
{{- $files := dict "nats-admin-creds" (list "nats-user-admin") -}}
{{- if include "lance.natsMints" . }}
{{- $_ := set $files "nats-box-creds" (list "nats-user-monitor") }}
{{- $_ := set $files "nats-auth" (list "nats-server" "nats-route") }}
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
{{- printf "kubernetes~%s" (include "lance.saAccount" .) -}}
{{- end -}}

{{- define "lance.saAccount" -}}
{{- $root := index . 0 -}}
{{- printf "system:serviceaccount:%s:%s-sa-%s" $root.Release.Namespace (include "lance.fullname" $root) (index . 1) -}}
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

{{- define "lance.oidcIssuer" -}}
{{- $issuer := required "auth.oidc.issuer must name the IdP every governed door trusts and the zones sign in at: the development Dex's issuer with dex.enabled, the platform's IdP otherwise" .Values.auth.oidc.issuer -}}
{{- if and (not .Values.dex.enabled) (not (hasPrefix "https://" $issuer)) -}}
{{- fail (printf "auth.oidc.issuer=%s is not https. Without the development Dex (dex.enabled=false) the issuer is the platform's IdP, and every governed door fetches the keys it checks each token against from it: over http, whoever answers that address on the path chooses them. Name the IdP's https issuer." $issuer) -}}
{{- end -}}
{{- $issuer -}}
{{- end -}}

{{- define "lance.oidcConnectors" -}}
{{- $connectors := deepCopy (.Values.auth.oidc.connectors | default dict) -}}
{{- if and .Values.dex.enabled (not (hasKey $connectors "local")) -}}
{{- $_ := set $connectors "local" "dex" -}}
{{- end -}}
{{- if not $connectors -}}
{{- fail "auth.oidc.connectors must name the idp id of each of the IdP's Dex connectors (e.g. {ldap: corp}): a person is the principal <idp-id>~<user id> their connector maps to, and a connector named nowhere signs nobody in" -}}
{{- end -}}
{{- range $connector, $idp := $connectors -}}
{{- if not $connector -}}
{{- fail "auth.oidc.connectors names an empty connector id: Dex names every connector" -}}
{{- end -}}
{{- if or (not (kindIs "string" $idp)) (eq $idp "kubernetes") (not (regexMatch "^[a-z0-9-]+$" $idp)) -}}
{{- fail (printf "auth.oidc.connectors.%s=%v is not an idp id: lower-case letters, digits and '-', never kubernetes, the service-account issuer's" $connector $idp) -}}
{{- end -}}
{{- end -}}
{{- toJson $connectors -}}
{{- end -}}

{{- define "lance.bootstrapAdmin" -}}
{{- $admin := .Values.auth.bootstrapAdmin | default "" | toString -}}
{{- if and $admin (or (hasPrefix "kubernetes~" $admin) (not (regexMatch "^[a-z0-9-]+~[^[:cntrl:]]+$" $admin))) -}}
{{- fail (printf "auth.bootstrapAdmin=%s is not a person's principal: name them <idp-id>~<user id>, the idp id auth.oidc.connectors gives their Dex connector and the user id it reports (dex~<userID> for a development Dex user)" $admin) -}}
{{- end -}}
{{- $admin -}}
{{- end -}}

{{- define "lance.oidcAudience" -}}
{{- $audience := required "auth.oidc.audience names the lakehouse's own audience, which every governed door checks" .Values.auth.oidc.audience -}}
{{- if eq $audience .Values.auth.oidc.clientId -}}
{{- fail (printf "auth.oidc.audience=%s is the zones' sign-in client (auth.oidc.clientId): the lakehouse checks an audience of its own, so a token the IdP issued for a client is refused" $audience) -}}
{{- end -}}
{{- $audience -}}
{{- end -}}

{{- define "lance.oidcAudienceScope" -}}
{{- .Values.auth.oidc.audienceScope | default (printf "audience:server:client_id:%s" (include "lance.oidcAudience" .)) -}}
{{- end -}}

{{- define "lance.governedOidcEnv" -}}
{{- $root := . }}
- { name: RASK_OIDC_ENABLED, value: "true" }
- { name: RASK_OIDC_ISSUER, value: {{ include "lance.oidcIssuer" $root | quote }} }
{{- if $root.Values.dex.enabled }}
- { name: RASK_OIDC_DISCOVERY_URL, value: "http://{{ include "lance.dexHost" $root }}:{{ $root.Values.dex.port }}/dex" }
{{- end }}
- { name: RASK_OIDC_AUDIENCE, value: {{ include "lance.oidcAudience" $root | quote }} }
- { name: RASK_OIDC_ALLOW_INSECURE, value: {{ ternary "true" "false" $root.Values.dex.enabled | quote }} }
- { name: RASK_OIDC_CONNECTORS, value: {{ include "lance.oidcConnectors" $root | quote }} }
{{- end -}}

{{- define "lance.governedFgaEnv" -}}
{{- $root := index . 0 }}{{- $name := index . 1 }}
- { name: RASK_FGA_ENABLED, value: "true" }
{{- if include "lance.isFgaWriter" (list $root $name) }}
- { name: RASK_FGA_API_URL, value: {{ include "lance.openfgaUrl" $root | quote }} }
{{- include "lance.governedFgaPins" $root }}
{{- end }}
{{- include "lance.fgaCredential" (list $root "env" $name) }}
{{- end -}}

{{- define "lance.openfgaAudience" -}}rask-openfga{{- end -}}

{{- define "lance.fgaWriters" -}}
catalog maintenance
{{- end -}}

{{- define "lance.isFgaWriter" -}}
{{- if has (index . 1) (include "lance.fgaWriters" (index . 0) | splitList " ") }}true{{ end -}}
{{- end -}}

{{- define "lance.openfgaSubjects" -}}
{{- $v := .Values -}}{{- $p := printf "system:serviceaccount:%s:" .Release.Namespace -}}{{- $sa := printf "%s%s-sa-" $p (include "lance.fullname" .) -}}
{{- $out := list -}}
{{- if $v.auth.enabled -}}
{{- range list "catalog" "jobs" "hooks" }}{{ $out = append $out (printf "%s%s" $sa .) }}{{ end -}}
{{- if $v.maintenance.enabled }}{{ $out = append $out (printf "%smaintenance" $sa) }}{{ end -}}
{{- end -}}
{{- $out | uniq | sortAlpha | join "," -}}
{{- end -}}

{{- define "lance.fgaDecisionRoles" -}}
{{- $v := .Values -}}{{- $root := . -}}
{{- $routes := dict "lineage" (list "governance_prober") "notifications" (list "member_expander") -}}
{{- $names := list "lineage" -}}
{{- if $v.explorer.enabled }}{{ range $n, $_ := $v.explorer.services }}{{ $names = append $names $n }}{{ end }}{{ end -}}
{{- range $n, $s := $v.services }}{{ if and (not (has $n (list "catalog" "lineage"))) (or $s.frontDoor $v.singleTenant.enabled) $s.governedAuth }}{{ $names = append $names $n }}{{ end }}{{ end -}}
{{- $out := dict -}}
{{- if $v.auth.enabled -}}
{{- range ($names | uniq) }}{{ if not (include "lance.isFgaWriter" (list $root .)) }}{{ $_ := set $out (include "lance.saSubject" (list $root .)) (prepend (get $routes . | default list) "decider") }}{{ end }}{{ end -}}
{{- range ($v.auth.externalDeciders | default list) }}{{- if not (regexMatch "^[a-z0-9-]+:[a-z0-9.-]+$" .) }}{{- fail (printf "auth.externalDeciders entry %q is not <namespace>:<serviceaccount>" .) }}{{- end }}{{ $_ := set $out (printf "kubernetes~system:serviceaccount:%s" .) (list "decider") }}{{ end -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{- define "lance.catalogUrl" -}}
{{- printf "http://%s-catalog:%v" (include "lance.fullname" .) .Values.services.catalog.port -}}
{{- end -}}

{{- define "lance.fgaCredential" -}}
{{- $root := index . 0 -}}{{- $part := index . 1 -}}{{- $name := index . 2 -}}
{{- if include "lance.isFgaWriter" (list $root $name) }}
{{- include "lance.fgaToken" (list $part) }}
{{- else if eq $part "env" }}
- { name: RASK_FGA_DECIDER_URL, value: {{ include "lance.catalogUrl" $root | quote }} }
- { name: RASK_FGA_DECIDER_TOKEN_FILE, value: /var/run/secrets/rask/identity/rask-catalog/token }
{{- else if not (and (gt (len .) 3) (index . 3)) }}
{{- include "lance.identityTokens" (list $part "rask-catalog") }}
{{- end }}
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

{{- define "lance.objectStoreAudience" -}}
{{- required "objectStore.identity.audience names the audience the catalog's store token is projected for, which the store trusts as its identity provider's client; the catalog holds no store key (ADR 0170)" .Values.objectStore.identity.audience -}}
{{- end -}}

{{- define "lance.mediaIdentity" -}}
{{- if and .Values.explorer.enabled .Values.explorer.corpusS3Root -}}
{{- required "objectStore.mediaAccessKey names search's read-only identity on explorer.corpusS3Root; it never presents the store's root (ADR 0168)" .Values.objectStore.mediaAccessKey -}}
{{- end -}}
{{- end -}}

{{- define "lance.backupIdentity" -}}
{{- if or .Values.backups.pgDump.enabled .Values.backups.controlRoot.enabled -}}
{{- required "objectStore.backupAccessKey names the backups' identity; a backup never holds the store's root (ADR 0168)" .Values.objectStore.backupAccessKey -}}
{{- end -}}
{{- end -}}

{{- define "lance.scopedSecretFn" -}}
scoped_secret() {
  s=$(printf '%s-s3-%s' "$1" "$root" | sha256sum | cut -c1-40)
  [ ${#s} -eq 40 ] || { echo "could not derive the scoped secret for $1" >&2; exit 1; }
  (umask 077; printf '%s' "$s" > "$2")
}
{{- end -}}

{{- define "lance.storeReads" -}}
{{- $v := .Values -}}{{- $root := . -}}
{{- $reads := dict -}}
{{- if $v.dapr.enabled -}}
{{- $sa := printf "%s-sa-" (include "lance.fullname" .) -}}
{{- $state := ternary ($v.stateStore.scopes | default list) list (and $v.stateStore.enabled true) -}}
{{- $doors := ne (($v.signing).doors | toString) "off" -}}
{{- $everySigner := list -}}
{{- range (include "lance.signingKeys" (list .) | splitList " " | compact) }}{{- $everySigner = append $everySigner (printf "signing-public-%s" .) }}{{- end -}}
{{- $own := dict -}}
{{- range (include "lance.signers" . | fromJsonArray) }}{{- $_ := set $own .app (concat (get $own .app | default list) (list (printf "signing-key-%s" .key) (printf "signing-public-%s" .key))) }}{{- end -}}
{{- $apps := list
  (dict "app" $v.services.catalog.daprAppId "sa" "catalog" "api" (concat (list "dapr-app-token") (ternary $everySigner list (and $v.auth.enabled $doors $v.catalog.controlEmit))) "bus" true)
  (dict "app" $v.services.lineage.daprAppId "sa" "lineage" "api" (concat (list "lineage-s3" "dapr-app-token") (compact (list $v.services.lineage.forward.authSecret.name)) (ternary $everySigner list (and $v.auth.enabled ($v.signing).enforce))) "bus" true) -}}
{{- if $v.maintenance.enabled -}}
{{- $apps = append $apps (dict "app" $v.maintenance.daprAppId "sa" "maintenance" "api" (concat (list "maintenance-s3" "dapr-app-token") (ternary $everySigner list (and $v.auth.enabled $doors $v.maintenance.arrivalEnabled))) "bus" (or $v.maintenance.arrivalEnabled $v.maintenance.lineageEmit (and $v.catalog.controlEmit $v.maintenance.controlEmit))) -}}
{{- end -}}
{{- if $v.explorer.enabled -}}
{{- range $name, $svc := $v.explorer.services -}}
{{- $api := list -}}
{{- if and (eq $name "viewer") $v.storage.stores }}{{- range $v.storage.stores }}{{- $api = append $api .secret }}{{- end }}{{- end -}}
{{- if $v.explorer.corpusS3Root }}{{- $api = append $api (ternary "viewer-s3" "media-s3" (eq $name "viewer")) }}{{- end -}}
{{- $apps = append $apps (dict "app" ($svc.daprAppId | default $name) "sa" $name "api" $api "bus" false) -}}
{{- end -}}
{{- end -}}
{{- range $name, $svc := $v.services -}}
{{- if and (not (has $name (list "catalog" "lineage"))) (or $svc.frontDoor $v.singleTenant.enabled) -}}
{{- $api := ternary (list "dapr-app-token") list ($svc.daprIngest | default false) -}}
{{- $channels := list -}}
{{- if eq $name "notifications" }}
{{- $api = concat $api (ternary $everySigner list (and $v.auth.enabled $doors)) }}
{{- range $channel := list "email" "slack" }}{{- with get ($svc.channels | default dict) $channel }}{{- if .enabled }}{{- $channels = append $channels .secretName }}{{- end }}{{- end }}{{- end }}
{{- end -}}
{{- $apps = append $apps (dict "app" $name "sa" $name "api" $api "bus" (eq $name "notifications") "channels" $channels) -}}
{{- end -}}
{{- end -}}
{{- range $apps -}}
{{- $api := concat .api (get $own .app | default list) | compact | uniq | sortAlpha -}}
{{- $components := concat (ternary (list (printf "nats-user-%s" .app)) list (and .bus true)) (ternary (list "dapr-state") list (has .app $state)) (.channels | default list) | compact | uniq | sortAlpha -}}
{{- if or $api $components -}}
{{- $_ := set $reads .app (dict "sa" (printf "%s%s" $sa .sa) "api" $api "components" $components) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- toJson $reads -}}
{{- end -}}

{{- define "lance.esoReads" -}}
{{- $v := .Values -}}
{{- $reads := list "postgres" -}}
{{- if $v.objectStore.enabled }}{{- $reads = append $reads "object-store-root" }}{{- end -}}
{{- if $v.observability.enabled }}{{- $reads = append $reads "otel-collector-export" }}{{- end -}}
{{- if $v.ray.auth.enabled }}{{- $reads = append $reads "ray-auth-token" }}{{- end -}}
{{- if include "lance.backupIdentity" . }}{{- $reads = append $reads "backup-s3" }}{{- end -}}
{{- if or $v.dapr.enabled $v.auth.enabled }}{{- $reads = append $reads "dapr-app-token" }}{{- end -}}
{{- if and $v.frontend.enabled $v.auth.enabled $v.frontend.oidc.enabled }}{{- $reads = concat $reads (list "frontend-session" "oidc-client") }}{{- end -}}
{{- if $v.secrets.hfTokenInStore }}{{- $reads = append $reads "hf-token" }}{{- end -}}
{{- range $files := include "lance.natsFiles" . | fromJson }}{{- $reads = concat $reads $files }}{{- end -}}
{{- toJson ($reads | uniq | sortAlpha) -}}
{{- end -}}

{{- define "lance.storeRole" -}}
{{- printf "%s-%s" (include "lance.fullname" (index . 0)) (index . 1) -}}
{{- end -}}

{{- define "lance.storePolicy" -}}
{{- $mount := (index . 0).Values.externalSecrets.kvMount -}}
{{- $paths := dict "auth/token/lookup-self" (dict "capabilities" (list "read")) "auth/token/renew-self" (dict "capabilities" (list "update")) -}}
{{- range (index . 1) }}{{- $_ := set $paths (printf "%s/data/%s" $mount .) (dict "capabilities" (list "read")) }}{{- end -}}
{{- toJson (dict "path" $paths) -}}
{{- end -}}

{{- define "lance.storeIdentity" -}}
{{- $root := index . 0 -}}{{- $appId := index . 1 -}}{{- $part := index . 2 -}}
{{- if hasKey (include "lance.storeReads" $root | fromJson) $appId -}}
{{- if eq $part "annotation" }}
dapr.io/volume-mounts: "openbao-identity:/var/run/secrets/rask/openbao"
{{- else }}
- name: openbao-identity
  projected:
    sources:
      - serviceAccountToken: { path: token, audience: {{ include "lance.storeAudience" $root | quote }}, expirationSeconds: 600 }
{{- end }}
{{- end -}}
{{- end -}}

{{- define "lance.storeAudience" -}}
{{- required "openbao.identity.audience names the audience each sidecar's store token is projected for; a role bound to none admits any token its ServiceAccount holds" .Values.openbao.identity.audience -}}
{{- end -}}

{{- define "lance.secretScopes" -}}
{{- keys (include "lance.storeReads" . | fromJson) | sortAlpha | join "," -}}
{{- end -}}

{{- define "lance.appTokenEnv" -}}
{{- $root := index . 0 -}}{{- $appId := index . 1 -}}
{{- if has $appId (splitList "," (include "lance.secretScopes" $root)) -}}
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
        defaultAccess: deny
        {{- with (get (include "lance.storeReads" $cfgRoot | fromJson) $app).api }}
        allowedSecrets:
          {{- range . }}
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
{{- range (include "lance.signingIdentitiesForApp" . | splitList " " | compact) }}- { name: RASK_SIGNING_IDENTITY, value: {{ . | quote }} }{{- end }}
{{- end -}}

{{- define "lance.controlSignerRoles" -}}
{{- $m := dict "catalog" (list) "maintenance" (list) -}}
{{- range (include "lance.signers" . | fromJsonArray) }}{{- if .role }}{{- $_ := set $m .role (append (get $m .role) .id | uniq | sortAlpha) }}{{- end }}{{- end -}}
{{- toJson $m -}}
{{- end -}}

{{- define "lance.doorEnv" -}}
{{- $r := index . 0 -}}{{- $mode := ($r.Values.signing).doors -}}
{{- if not (has $mode (list "off" "observe" "enforce")) }}{{- fail (printf "signing.doors is %v, not the string off, observe or enforce" $mode) }}{{- end }}
{{- if $r.Values.auth.enabled }}
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
{{- $buckets := .Values.objectStore.buckets | default list -}}
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
{{ .Values.vending.externalBlobBases | default (printf "s3://%s/models/" .Values.objectStore.bucket) }}
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

{{- define "rask.catalogPublicPrefix" -}}/api/catalog{{- end -}}

{{- define "rask.apiRoutes" -}}
{{- $root := . -}}
{{- $rows := list -}}
{{- if .Values.explorer.enabled -}}
{{- $media := .Values.explorer.services -}}
{{- $rows = append $rows (dict "prefix" "/api/explorer/search" "component" "search" "service" (printf "%s-search" (include "lance.fullname" $root)) "port" $media.search.port "rewrite" "/api/search") -}}
{{- $rows = append $rows (dict "prefix" "/api/explorer" "component" "viewer" "service" (printf "%s-viewer" (include "lance.fullname" $root)) "port" $media.viewer.port "rewrite" "/api") -}}
{{- end -}}
{{- $rows = append $rows (dict "prefix" (include "rask.catalogPublicPrefix" $root) "component" "catalog" "service" (printf "%s-catalog" (include "lance.fullname" $root)) "port" .Values.services.catalog.port "rewrite" "") -}}
{{- $rows = append $rows (dict "prefix" "/api/lineage" "component" "lineage" "service" (printf "%s-lineage" (include "lance.fullname" $root)) "port" .Values.services.lineage.port "rewrite" "/") -}}
{{- $prefix := .Values.config.RASK_API_PREFIX -}}
{{- range $name := list "flows" "notifications" -}}
{{- $svc := index $root.Values.services $name -}}
{{- if or $svc.frontDoor $root.Values.singleTenant.enabled -}}
{{- $rows = append $rows (dict "prefix" (printf "%s/%s" $prefix $name) "component" $name "service" (printf "%s-%s" (include "rask.fullname" $root) $name) "port" $svc.port "rewrite" "") -}}
{{- end -}}
{{- end -}}
{{- toJson $rows -}}
{{- end -}}
