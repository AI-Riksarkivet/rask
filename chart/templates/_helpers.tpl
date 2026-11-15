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

{{- define "rask.otelResourceAttrs" -}}
{{- $o := .Values.observability -}}
{{- printf "service.namespace=rask,deployment.environment.name=%s,service.version=%s" ($o.environment | default .Release.Namespace) .Chart.AppVersion -}}
{{- end -}}

{{- define "lance.fullname" -}}{{ .Release.Name }}{{- end -}}

{{- define "rask.bootstrapRev" -}}r{{ .Release.Revision }}{{- end -}}

{{- define "lance.reconciledJob" -}}
argocd.argoproj.io/hook: {{ . }}
argocd.argoproj.io/hook-delete-policy: BeforeHookCreation
{{- end -}}

{{- define "lance.replicaBounds" -}}
{{- $root := index . 0 -}}
{{- $svc := index . 1 -}}
{{- $fixed := int ((hasKey $svc "replicas") | ternary $svc.replicas 1) -}}
{{- if $root.Values.autoscaling.enabled -}}
{{- dict "min" (int $root.Values.autoscaling.minReplicas) "max" (int $root.Values.autoscaling.maxReplicas) | toJson -}}
{{- else -}}
{{- dict "min" $fixed "max" $fixed | toJson -}}
{{- end -}}
{{- end -}}

{{- define "rask.isRealDeployment" -}}
{{- $repo := (.Values.image.repository | default "") -}}
{{- $sideloaded := or .Values.image.localImages (hasPrefix "localhost:" $repo) (hasPrefix "127.0.0.1:" $repo) (hasPrefix "172.17.0.1:" $repo) -}}
{{- if or (not .Values.openbao.enabled) (not $sideloaded) -}}true{{- end -}}
{{- end -}}

{{- define "lance.provenanceNamespace" -}}
{{- $ns := .Values.services.lineage.provenanceNamespace | default "" -}}
{{- if or (not (hasPrefix "lance_catalog$" $ns)) (eq $ns "lance_catalog$") -}}
{{- fail (printf "services.lineage.provenanceNamespace %q must be a namespace of the estate's own warehouse, lance_catalog$<namespace>: the first segment of every name is its warehouse, and the catalog refuses to boot on any other" $ns) -}}
{{- end -}}
{{- $ns -}}
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

{{- define "lance.tokenVerifiers" -}}
{{- if .Values.auth.enabled }}{{- list "catalog" "lineage" | toJson }}{{- else }}[]{{- end -}}
{{- end -}}

{{- define "lance.objectStoreClients" -}}
{{- $clients := list -}}
{{- range (include "lance.objectStoreIdentities" . | fromJsonArray) }}
{{- if ne . "maintenance" }}{{- $clients = append $clients . }}
{{- else }}{{- $clients = append $clients "maintenance-worker" }}{{- end }}
{{- end -}}
{{- toJson $clients -}}
{{- end -}}

{{- define "lance.busClients" -}}
{{- $clients := list "catalog" "lineage" -}}
{{- if .Values.maintenance.enabled }}{{- $clients = append $clients "maintenance-worker" }}{{- end -}}
{{- range $app, $_ := (.Values.openbao.externalReaders | default dict) }}{{- $clients = append $clients $app }}{{- end -}}
{{- toJson (append ($clients | uniq) "nats-stream") -}}
{{- end -}}

{{- define "lance.platformEgress" -}}
{{- $v := .Values -}}
{{- $rows := list -}}
{{- $api := include "lance.tokenVerifiers" . | fromJsonArray -}}
{{- if and $v.auth.issuerMirror.enabled (or $v.openfga.enabled $v.objectStore.enabled) }}{{- $api = append $api "issuer-mirror" }}{{- end -}}
{{- if $v.openbao.enabled }}{{- $api = append $api "openbao" }}{{- end -}}
{{- if not (include "rask.isRealDeployment" .) }}{{- $api = append $api "bus-signer" }}{{- end -}}
{{- if $v.observability.enabled }}{{- $api = append $api "otel-collector" }}{{- end -}}
{{- if $v.backups.volumeSnapshot.enabled }}{{- $api = append $api "object-store-backup" }}{{- end -}}
{{- if $api -}}
{{- $rows = append $rows (dict "name" "apiserver" "value" "networkPolicy.apiServerEgress" "rules" $v.networkPolicy.apiServerEgress "reaches" "the Kubernetes API server's endpoints, at the port a policy sees after the kubernetes Service's translation" "components" $api) -}}
{{- end -}}
{{- if not $v.openfga.enabled -}}
{{- $rows = append $rows (dict "name" "openfga" "value" "auth.fgaEgress" "rules" $v.auth.fgaEgress "reaches" "the platform's OpenFGA at auth.fgaApiUrl" "components" (concat (include "lance.fgaWriters" . | splitList " ") (list "openfga-model"))) -}}
{{- end -}}
{{- if not $v.openbao.enabled -}}
{{- $rows = append $rows (dict "name" "secret-store" "value" "openbao.egress" "rules" $v.openbao.egress "reaches" "the platform's secret store at openbao.externalAddr" "components" (splitList "," (include "lance.secretReaderComponents" .) | compact)) -}}
{{- end -}}
{{- if $v.observability.enabled -}}
{{- $rows = append $rows (dict "name" "telemetry-export" "value" "observability.export.egress" "rules" $v.observability.export.egress "reaches" "the telemetry backend at observability.export.endpoint" "components" (list "otel-collector")) -}}
{{- end -}}
{{- if not $v.objectStore.enabled -}}
{{- $rows = append $rows (dict "name" "object-store" "value" "objectStore.egress" "rules" $v.objectStore.egress "reaches" "the platform's S3 and its STS at objectStore.externalEndpoint" "components" (include "lance.objectStoreClients" . | fromJsonArray)) -}}
{{- end -}}
{{- if and $v.auth.enabled (not $v.dex.enabled) -}}
{{- $rows = append $rows (dict "name" "issuer" "value" "auth.oidc.egress" "rules" $v.auth.oidc.egress "reaches" "the platform's IdP at auth.oidc.issuer" "components" (include "lance.tokenVerifiers" . | fromJsonArray)) -}}
{{- end -}}
{{- if include "rask.isRealDeployment" . -}}
{{- $rows = append $rows (dict "name" "bus" "value" "nats.egress" "rules" $v.nats.egress "reaches" "the platform's NATS bus at nats.url" "components" (include "lance.busClients" . | fromJsonArray)) -}}
{{- if $v.services.lineage.forward.url -}}
{{- $rows = append $rows (dict "name" "lineage-graph" "value" "services.lineage.forward.egress" "rules" $v.services.lineage.forward.egress "reaches" "the graph backend at services.lineage.forward.url" "components" (list "lineage")) -}}
{{- end -}}
{{- end -}}
{{- toJson $rows -}}
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
{{- fail (printf "openbao.externalAddr is %q: every service sends its ServiceAccount token and its store token to it, so the platform's secret store is reached over https only" $addr) -}}
{{- end -}}
{{- $addr -}}
{{- end -}}

{{- define "lance.natsUrl" -}}
{{- required "NATS JetStream is a prerequisite: set nats.url to the bus every service and the stream Job connect to (docs/architecture/deployment.md, Prerequisites); rask never installs it" .Values.nats.url -}}
{{- end -}}

{{- define "lance.busEnv" -}}
{{- $root := index . 0 -}}{{- $user := index . 1 -}}
{{- if $user -}}
{{- include "lance.busSessionUser" (list $root $user) -}}
{{- $inbox := printf "_INBOX_%s.>" $user -}}
{{- if not (has $inbox ((get (include "lance.natsTable" $root | fromJson) $user).subscribe | default list)) }}{{- fail (printf "nats.auth.users.%s grants the service %s no inbox of its own (%s): the bus would hand it no acknowledgement of a publish, so every event it publishes would stay staged" $user $user $inbox) }}{{- end }}
- { name: RASK_BUS_URL, value: {{ include "lance.natsUrl" $root | quote }} }
- { name: RASK_BUS_USER, value: {{ $user | quote }} }
{{- end }}
- { name: RASK_BUS_TOKEN_FILE, value: {{ include "lance.busTokenFile" $root | quote }} }
- { name: RASK_BUS_SESSION_SECONDS, value: {{ include "lance.busSessionSeconds" $root | quote }} }
{{- if include "lance.busCaConfigMap" $root }}
- { name: RASK_BUS_CA_FILE, value: {{ include "lance.busCaFile" $root | quote }} }
{{- end }}
{{- end -}}

{{- define "lance.busSessionUser" -}}
{{- $root := index . 0 -}}{{- $user := index . 1 -}}
{{- if not (has $user (include "lance.natsUsers" $root | splitList " ")) }}{{- fail (printf "the service %s connects to the bus as the user %s, which nats.auth.users does not declare" $user $user) }}{{- end -}}
{{- if not (hasKey (include "lance.natsSessions" $root | fromJson) $user) }}{{- fail (printf "the service %s connects to the bus as the user %s, which is no session user: it holds no bus credential, only its ServiceAccount token" $user $user) }}{{- end -}}
{{- end -}}

{{- define "lance.busAudience" -}}
{{- $aud := required "nats.auth.audience names the audience each session user's pod projects its bus token for, which the signer's TokenReview names" .Values.nats.auth.audience | toString -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,52}[a-z0-9])?$" $aud) }}{{- fail (printf "nats.auth.audience %q must be a lowercase name: it names the pod's token volume" $aud) }}{{- end -}}
{{- $aud -}}
{{- end -}}

{{- define "lance.busTokenFile" -}}/var/run/secrets/rask/identity/{{ include "lance.busAudience" . }}/token{{- end -}}

{{- define "lance.busSessionSeconds" -}}
{{- $s := required "nats.auth.sessionSeconds is the longest a bus session lives" .Values.nats.auth.sessionSeconds -}}
{{- if not (kindIs "float64" $s) }}{{- if not (kindIs "int64" $s) }}{{- if not (kindIs "int" $s) }}{{- fail (printf "nats.auth.sessionSeconds is %v: a number of seconds" $s) }}{{- end }}{{- end }}{{- end -}}
{{- $n := int $s -}}
{{- if lt $n 60 }}{{- fail (printf "nats.auth.sessionSeconds is %d: a session lives at least a minute, so a pod's refresh never races its own connect" $n) }}{{- end -}}
{{- if ge $n 600 }}{{- fail (printf "nats.auth.sessionSeconds is %d: a session must end before the 600 s token the pod presents for it, or a refresh would present a token about to lapse" $n) }}{{- end -}}
{{- $n -}}
{{- end -}}

{{- define "lance.busCaFile" -}}/etc/rask/bus-ca/ca.crt{{- end -}}

{{- define "lance.busCaConfigMap" -}}
{{- $ca := .Values.nats.ca | default dict -}}
{{- if $ca.configMap -}}
{{- if not $ca.key }}{{- fail "nats.ca.key names the key of the ConfigMap nats.ca.configMap that holds the bus's CA bundle" }}{{- end -}}
{{- $ca.configMap -}}
{{- end -}}
{{- end -}}

{{- define "lance.busIdentity" -}}
{{- $root := index . 0 -}}{{- $part := index . 1 -}}
{{- $ca := include "lance.busCaConfigMap" $root -}}
{{- include "lance.identityTokens" (list $part (include "lance.busAudience" $root)) }}
{{- if $ca }}
{{- if eq $part "mounts" }}
- { name: bus-ca, mountPath: {{ dir (include "lance.busCaFile" $root) }}, readOnly: true }
{{- else }}
- name: bus-ca
  configMap:
    name: {{ $ca | quote }}
    items: [{ key: {{ $root.Values.nats.ca.key | quote }}, path: {{ base (include "lance.busCaFile" $root) }} }]
{{- end }}
{{- end }}
{{- end -}}

{{- define "lance.natsTable" -}}
{{- $t := deepCopy (.Values.nats.auth.users | default dict) -}}
{{- range $paths, $grants := (.Values.nats.auth.flagged | default dict) -}}
{{- $on := true -}}
{{- range $path := splitList "," $paths -}}
{{- $v := $.Values -}}
{{- range $k := splitList "." $path }}{{- if kindIs "map" $v }}{{- $v = get $v $k }}{{- else }}{{- $v = false }}{{- end }}{{- end -}}
{{- if not $v }}{{- $on = false }}{{- end -}}
{{- end -}}
{{- if $on -}}
{{- range $u, $g := $grants -}}
{{- $row := deepCopy (get $t $u | default dict) -}}
{{- $_ := set $row "publish" (concat ($row.publish | default list) ($g.publish | default list) | uniq) -}}
{{- $_ := set $row "subscribe" (concat ($row.subscribe | default list) ($g.subscribe | default list) | uniq) -}}
{{- $_ := set $t $u $row -}}
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

{{- define "lance.natsOwnAccounts" -}}
{{- $v := .Values -}}
{{- toJson (dict $v.services.catalog.appId "catalog" $v.services.lineage.appId "lineage" $v.maintenance.appId "maintenance" "maintenance-worker" "maintenance" "admin" "jobs") -}}
{{- end -}}

{{- define "lance.natsSessions" -}}
{{- $root := . -}}
{{- $own := include "lance.natsOwnAccounts" . | fromJson -}}
{{- $lakehouse := append (include "lance.serviceAccounts" . | fromJsonArray) "default" -}}
{{- $out := dict -}}
{{- $table := include "lance.natsTable" . | fromJson -}}
{{- range $u := (include "lance.natsUsers" . | splitList " " | compact) -}}
{{- $row := get $table $u -}}
{{- if hasKey $own $u -}}
{{- if hasKey $row "serviceAccount" }}{{- fail (printf "nats.auth.users.%s.serviceAccount: the lakehouse's own bus user takes its ServiceAccount from the chart, never from values" $u) }}{{- end -}}
{{- $_ := set $out $u (include "lance.saName" (list $root (get $own $u))) -}}
{{- else if hasKey $row "serviceAccount" -}}
{{- $sa := $row.serviceAccount | default "" | toString -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,251}[a-z0-9])?$" $sa) }}{{- fail (printf "nats.auth.users.%s.serviceAccount names no ServiceAccount" $u) }}{{- end -}}
{{- if has $sa $lakehouse }}{{- fail (printf "nats.auth.users.%s.serviceAccount: %q is the lakehouse's own ServiceAccount or the namespace's shared default: a session user's token names an identity of its own" $u $sa) }}{{- end -}}
{{- $_ := set $out $u $sa -}}
{{- end -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{- define "lance.natsSessionUsers" -}}
{{- keys (include "lance.natsSessions" . | fromJson) | sortAlpha | join " " -}}
{{- end -}}

{{- define "lance.natsStaticUsers" -}}
{{- $sessions := include "lance.natsSessions" . | fromJson -}}
{{- $out := list -}}
{{- range (include "lance.natsUsers" . | splitList " " | compact) }}{{- if not (hasKey $sessions .) }}{{- $out = append $out . }}{{- end }}{{- end -}}
{{- join " " $out -}}
{{- end -}}

{{- define "lance.natsPermissions" -}}
{{- $root := . -}}
{{- $table := include "lance.natsTable" . | fromJson -}}
{{- $users := dict -}}
{{- range $u, $sa := (include "lance.natsSessions" . | fromJson) -}}
{{- $row := get $table $u -}}
{{- $_ := set $users $u (dict "serviceAccount" (include "lance.accountOf" (list $root $sa)) "publish" $row.publish "subscribe" $row.subscribe) -}}
{{- end -}}
{{- toPrettyJson (dict "audience" (include "lance.busAudience" .) "sessionSeconds" (include "lance.busSessionSeconds" . | int) "users" $users) -}}
{{- end -}}

{{- define "lance.busConsumers" -}}
{{- $v := .Values -}}
{{- $mark := dict "rask.dev/release" .Release.Name -}}
{{- $delivery := required "services.lineage.delivery is unset — upgrade with --reset-then-reuse-values, not --reuse-values" $v.services.lineage.delivery -}}
{{- $out := list (dict "stream" "LINEAGE" "name" "lineage-ingest" "start" "position" "config" (dict
  "durable_name" "lineage-ingest" "ack_policy" "explicit" "ack_wait" (mul (int (required "services.lineage.delivery.ackWaitSeconds is required" $delivery.ackWaitSeconds)) 1000000000)
  "max_deliver" -1 "filter_subject" $v.pubsub.topic "deliver_policy" "by_start_sequence" "replay_policy" "instant"
  "max_ack_pending" (int (required "services.lineage.deliveriesInFlight is required" $v.services.lineage.deliveriesInFlight)) "max_waiting" 512 "metadata" $mark)) -}}
{{- if $v.maintenance.enabled -}}
{{- $queue := required "maintenance.queue is unset — upgrade with --reset-then-reuse-values, not --reuse-values" $v.maintenance.queue -}}
{{- $ackWait := mul (int (required "maintenance.queue.ackWaitSeconds is required" $queue.ackWaitSeconds)) 1000000000 -}}
{{- $lastDelivery := add1 (int (required "maintenance.queue.maxDeliver is required" $queue.maxDeliver)) -}}
{{- $out = append $out (dict "stream" "MAINTENANCE_TASKS" "name" "maintenance-worker" "config" (dict
  "durable_name" "maintenance-worker" "ack_policy" "explicit" "ack_wait" $ackWait "max_deliver" $lastDelivery "filter_subject" "maintenance.task.>"
  "deliver_policy" "all" "replay_policy" "instant" "max_ack_pending" 1000 "max_waiting" 512 "metadata" $mark)) -}}
{{- $out = append $out (dict "stream" "MAINTENANCE_SCHEDULES" "name" "maintenance-due" "config" (dict
  "durable_name" "maintenance-due" "ack_policy" "explicit" "ack_wait" 30000000000 "max_deliver" -1 "filter_subject" "maintenance.due.>"
  "deliver_policy" "all" "replay_policy" "instant" "max_ack_pending" 1000 "max_waiting" 512 "metadata" $mark)) -}}
{{- $out = append $out (dict "stream" "STORE_EVENTS" "name" "maintenance-store-events" "config" (dict
  "durable_name" "maintenance-store-events" "ack_policy" "explicit" "ack_wait" 30000000000 "max_deliver" $lastDelivery "filter_subject" "store.events"
  "deliver_policy" "all" "replay_policy" "instant" "max_ack_pending" 1000 "max_waiting" 512 "metadata" $mark)) -}}
{{- if $v.maintenance.arrivalEnabled -}}
{{- $out = append $out (dict "stream" "LINEAGE" "name" "maintenance-arrival" "config" (dict
  "durable_name" "maintenance-arrival" "ack_policy" "explicit" "ack_wait" 30000000000 "max_deliver" $lastDelivery "filter_subject" $v.pubsub.topic
  "deliver_policy" "new" "replay_policy" "instant" "max_ack_pending" 1000 "max_waiting" 512 "metadata" $mark)) -}}
{{- end -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{- define "lance.mintsDevCredentials" -}}
{{- if .Values.openbao.enabled -}}true{{- end -}}
{{- end -}}

{{- define "lance.fgaMaintainers" -}}
{{- $out := list -}}
{{- if and .Values.maintenance.enabled .Values.maintenance.vendWriteCredentials -}}
{{- $out = append $out (include "lance.saSubject" (list . "maintenance")) -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- define "lance.devCredentials" -}}
{{- $root := . -}}
{{- $out := dict -}}
{{- range $key := list "object-store-secret-key" "postgres-password" -}}
{{- $_ := set $out $key (printf "%s-dev-credential-%s" (include "lance.fullname" $root) $key) -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{- define "lance.natsMints" -}}
{{- include "lance.mintsDevCredentials" . -}}
{{- end -}}

{{- define "lance.natsFiles" -}}
{{- $files := dict -}}
{{- if include "lance.natsMints" . }}
{{- $_ := set $files "nats-box-creds" (list "nats-user-monitor") }}
{{- $_ := set $files "nats-auth" (list "nats-server" "nats-route") }}
{{- $_ := set $files "nats-signer" (list "nats-callout") }}
{{- if .Values.objectStore.enabled }}{{- $_ := set $files "object-store-bus-creds" (list "nats-user-object-store") }}{{- end }}
{{- end -}}
{{- toJson $files -}}
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

{{- define "lance.podSecurityContext" -}}
securityContext:
  runAsNonRoot: true
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

{{- define "lance.accountOf" -}}
{{- $root := index . 0 -}}
{{- printf "system:serviceaccount:%s:%s" $root.Release.Namespace (index . 1) -}}
{{- end -}}

{{- define "lance.subjectOf" -}}
{{- printf "kubernetes~%s" (include "lance.accountOf" .) -}}
{{- end -}}

{{- define "lance.saName" -}}
{{- printf "%s-sa-%s" (include "lance.fullname" (index . 0)) (index . 1) -}}
{{- end -}}

{{- define "lance.saSubject" -}}
{{- include "lance.subjectOf" (list (index . 0) (include "lance.saName" .)) -}}
{{- end -}}

{{- define "lance.saComponents" -}}
{{- $components := list "catalog" "lineage" "maintenance" "jobs" "issuer-mirror" "hooks" -}}
{{- if .Values.age.enabled }}{{- $components = append $components "age" }}{{- end -}}
{{- if .Values.backups.pgDump.enabled }}{{- $components = append $components "backup-pg" }}{{- end -}}
{{- if .Values.backups.controlRoot.enabled }}{{- $components = append $components "backup-control-root" }}{{- end -}}
{{- toJson $components -}}
{{- end -}}

{{- define "lance.objectStoreIdentities" -}}
{{- $root := . -}}
{{- $identities := list -}}
{{- range (include "lance.saComponents" . | fromJsonArray) }}
{{- if and (has . (list "catalog" "lineage" "maintenance" "backup-pg" "backup-control-root")) (or (ne . "maintenance") $root.Values.maintenance.enabled) }}{{- $identities = append $identities . }}{{- end }}
{{- end -}}
{{- toJson $identities -}}
{{- end -}}

{{- define "lance.objectStorePolicy" -}}
{{- $statements := include (printf "lance.objectStorePolicy.%s" (index . 1)) (index . 0) | fromJsonArray -}}
{{- toPrettyJson (dict "Version" "2012-10-17" "Statement" $statements) -}}
{{- end -}}

{{- define "lance.objectStorePolicy.catalog" -}}
{{- toJson (list (dict "Sid" "TheWholeDataPlaneItGoverns" "Effect" "Allow" "Action" (list "s3:ListBucket" "s3:GetBucketLocation" "s3:ListAllMyBuckets" "s3:GetObject" "s3:PutObject" "s3:DeleteObject" "s3:AbortMultipartUpload" "s3:CreateBucket" "s3:DeleteBucket") "Resource" (list "arn:aws:s3:::*" "arn:aws:s3:::*/*"))) -}}
{{- end -}}

{{- define "lance.objectStorePolicy.lineage" -}}
{{- $control := .Values.objectStore.bucket -}}
{{- $statements := list
  (dict "Sid" "FindTheBucket" "Effect" "Allow" "Action" (list "s3:GetBucketLocation") "Resource" (list (printf "arn:aws:s3:::%s" $control)))
  (dict "Sid" "ListItsOwnOutbox" "Effect" "Allow" "Action" (list "s3:ListBucket") "Resource" (list (printf "arn:aws:s3:::%s" $control)) "Condition" (dict "StringLike" (dict "s3:prefix" (list "_lineage_outbox" "_lineage_outbox/" "_lineage_outbox/*"))))
  (dict "Sid" "DrainItsOwnOutboxAndNothingElse" "Effect" "Allow" "Action" (list "s3:GetObject" "s3:DeleteObject") "Resource" (list (printf "arn:aws:s3:::%s/_lineage_outbox" $control) (printf "arn:aws:s3:::%s/_lineage_outbox/*" $control))) -}}
{{- if .Values.services.lineage.relay.enabled -}}
{{- $statements = append $statements (dict "Sid" "ClaimItsOwnTimersWindows" "Effect" "Allow" "Action" (list "s3:GetObject" "s3:PutObject") "Resource" (list (printf "arn:aws:s3:::%s/_timers/lineage-relay.json" $control))) -}}
{{- end -}}
{{- toJson $statements -}}
{{- end -}}

{{- /* Each governed bucket's event configuration is read by the hourly audit (`maintenance.services.bucket_events`): the
action RustFS 1.0.3 checks is `s3:GetBucketNotification`, on the bucket's own resource (measured 2026-11-10). The governed
buckets include every warehouse bucket the catalog creates at runtime, which no render can name, so it is granted on the
buckets as the estate's reads already are. */ -}}
{{- define "lance.objectStorePolicy.maintenance" -}}
{{- $control := .Values.objectStore.bucket -}}
{{- $own := list (printf "arn:aws:s3:::%s/_maintenance_due/*" $control) (printf "arn:aws:s3:::%s/_maintenance_files/*" $control) (printf "arn:aws:s3:::%s/_policies/state/*" $control) (printf "arn:aws:s3:::%s/_lineage_outbox/*" $control) -}}
{{- $judged := list -}}
{{- range (list "_locations" "_trash" "_policies" "_warehouses" "_projects" "_bases" "_file_identities" "_maintenance_due" "_maintenance_files" "_lineage_outbox") }}{{- $judged = append $judged (printf "arn:aws:s3:::%s/%s/*" $control .) }}{{- end -}}
{{- toJson (list
  (dict "Sid" "ListAccountForReconcileDriftReport" "Effect" "Allow" "Action" (list "s3:ListAllMyBuckets") "Resource" (list "arn:aws:s3:::*"))
  (dict "Sid" "ListEveryBucketItSweeps" "Effect" "Allow" "Action" (list "s3:ListBucket" "s3:GetBucketLocation") "Resource" (list "arn:aws:s3:::*"))
  (dict "Sid" "ReadEachTablesHistoryAndNoData" "Effect" "Allow" "Action" (list "s3:GetObject") "Resource" (list "arn:aws:s3:::*/_versions" "arn:aws:s3:::*/_versions/*.manifest" "arn:aws:s3:::*/_refs/*.json" "arn:aws:s3:::*/_transactions/*.txn" "arn:aws:s3:::*/__manifest/*" "arn:aws:s3:::*/tree" "arn:aws:s3:::*/_mem_wal"))
  (dict "Sid" "ReadTheRecordsItJudgesBy" "Effect" "Allow" "Action" (list "s3:GetObject") "Resource" $judged)
  (dict "Sid" "ReadEachBucketsEventConfiguration" "Effect" "Allow" "Action" (list "s3:GetBucketNotification") "Resource" (list "arn:aws:s3:::*"))
  (dict "Sid" "WriteItsOwnRecordsAndNothingElse" "Effect" "Allow" "Action" (list "s3:PutObject" "s3:DeleteObject") "Resource" $own)) -}}
{{- end -}}

{{- define "lance.objectStorePolicy.backup-pg" -}}
{{- $control := .Values.objectStore.bucket -}}
{{- toJson (list
  (dict "Sid" "FindTheBucket" "Effect" "Allow" "Action" (list "s3:GetBucketLocation") "Resource" (list (printf "arn:aws:s3:::%s" $control)))
  (dict "Sid" "ListItsOwnDumps" "Effect" "Allow" "Action" (list "s3:ListBucket") "Resource" (list (printf "arn:aws:s3:::%s" $control)) "Condition" (dict "StringLike" (dict "s3:prefix" (list "_backups/pg/" "_backups/pg/*"))))
  (dict "Sid" "WriteAndPruneItsOwnDumps" "Effect" "Allow" "Action" (list "s3:GetObject" "s3:PutObject" "s3:DeleteObject" "s3:AbortMultipartUpload" "s3:ListMultipartUploadParts") "Resource" (list (printf "arn:aws:s3:::%s/_backups/pg/*" $control)))) -}}
{{- end -}}

{{- define "lance.objectStorePolicy.backup-control-root" -}}
{{- $control := .Values.objectStore.bucket -}}
{{- $records := list "_projects" "_warehouses" "_trash" "_protection" "_policies" "_gates" "_bases" -}}
{{- $dest := splitList "/" (trimSuffix "/" (trimPrefix "s3://" (.Values.backups.controlRoot.dest | default (printf "s3://%s/_backups/control" $control)))) | compact -}}
{{- $destBucket := first $dest -}}{{- $destPrefix := join "/" (rest $dest) -}}
{{- $listed := list -}}{{- $read := list -}}
{{- range $records }}{{- $listed = concat $listed (list . (printf "%s/" .) (printf "%s/*" .)) }}{{- $read = append $read (printf "arn:aws:s3:::%s/%s/*" $control .) }}{{- end -}}
{{- $listDest := dict "Sid" "ListItsOwnBackups" "Effect" "Allow" "Action" (list "s3:ListBucket") "Resource" (list (printf "arn:aws:s3:::%s" $destBucket)) -}}
{{- if $destPrefix }}{{- $_ := set $listDest "Condition" (dict "StringLike" (dict "s3:prefix" (list (printf "%s/" $destPrefix) (printf "%s/*" $destPrefix)))) }}{{- end -}}
{{- toJson (list
  (dict "Sid" "FindTheBuckets" "Effect" "Allow" "Action" (list "s3:GetBucketLocation") "Resource" (list (printf "arn:aws:s3:::%s" $control) (printf "arn:aws:s3:::%s" $destBucket) | uniq))
  (dict "Sid" "ListTheRecordsItCopies" "Effect" "Allow" "Action" (list "s3:ListBucket") "Resource" (list (printf "arn:aws:s3:::%s" $control)) "Condition" (dict "StringLike" (dict "s3:prefix" $listed)))
  (dict "Sid" "ReadTheRecordsItCopies" "Effect" "Allow" "Action" (list "s3:GetObject") "Resource" $read)
  $listDest
  (dict "Sid" "WriteAndPruneItsOwnBackups" "Effect" "Allow" "Action" (list "s3:GetObject" "s3:PutObject" "s3:DeleteObject" "s3:AbortMultipartUpload" "s3:ListMultipartUploadParts") "Resource" (list (printf "arn:aws:s3:::%s/*" (join "/" (compact (list $destBucket $destPrefix))))))) -}}
{{- end -}}

{{- define "lance.objectStoreSessionEnv" -}}
{{- $root := index . 0 -}}{{- $prefix := index . 1 -}}{{- $component := index . 2 -}}
{{- $identity := $root.Values.objectStore.identity -}}
- { name: {{ $prefix }}_S3_WEB_IDENTITY_TOKEN_FILE, value: "/var/run/secrets/rask/identity/{{ include "lance.objectStoreAudience" $root }}/token" }
- { name: {{ $prefix }}_S3_STS_ENDPOINT, value: {{ include "lance.s3Endpoint" $root | quote }} }
- { name: {{ $prefix }}_S3_ASSUME_ROLE_ARN, value: {{ required (printf "objectStore.identity.roleArns.%s names the role the %s token is exchanged for" $component $component) (index $identity.roleArns $component) | quote }} }
- { name: {{ $prefix }}_S3_SESSION_SECONDS, value: {{ required "objectStore.identity.sessionSeconds is how long each store session lasts" $identity.sessionSeconds | quote }} }
{{- end -}}

{{- define "lance.extraStoreIdentities" -}}
{{- $root := . -}}
{{- $own := include "lance.serviceAccounts" . | fromJsonArray -}}
{{- $names := list -}}
{{- range $name, $_ := .Values.objectStore.identity.extra | default dict }}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$" $name) }}{{- fail (printf "objectStore.identity.extra: %q names no ServiceAccount" $name) }}{{- end -}}
{{- if or (has $name $own) (hasPrefix (printf "%s-sa-" (include "lance.fullname" $root)) $name) (eq $name "default") }}{{- fail (printf "objectStore.identity.extra: %q is the lakehouse's own ServiceAccount, whose policy the chart alone grants" $name) }}{{- end -}}
{{- $names = append $names $name -}}
{{- end -}}
{{- toJson $names -}}
{{- end -}}

{{- define "lance.extraStorePolicy" -}}
{{- toPrettyJson (dict "Version" "2012-10-17" "Statement" (index (index . 0).Values.objectStore.identity.extra (index . 1))) -}}
{{- end -}}

{{- define "lance.serviceAccounts" -}}
{{- $root := . -}}{{- $fullname := include "lance.fullname" . -}}
{{- $names := list (printf "%s-snapshot" $fullname) (printf "%s-otel-collector" $fullname) (printf "%s-openfga" .Release.Name) -}}
{{- range (include "lance.saComponents" . | fromJsonArray) }}{{- $names = append $names (include "lance.saName" (list $root .)) }}{{- end -}}
{{- if .Values.console.enabled }}{{- $names = append $names (include "lance.saName" (list $root "console")) }}{{- end -}}
{{- toJson $names -}}
{{- end -}}

{{- define "lance.saAccount" -}}
{{- include "lance.accountOf" (list (index . 0) (include "lance.saName" .)) -}}
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
{{- $issuer := required "auth.oidc.issuer must name the IdP every governed door trusts: the development Dex's issuer with dex.enabled, the platform's IdP otherwise" .Values.auth.oidc.issuer -}}
{{- if and (not .Values.dex.enabled) (not (hasPrefix "https://" $issuer)) -}}
{{- fail (printf "auth.oidc.issuer=%s is not https. Without the development Dex (dex.enabled=false) the issuer is the platform's IdP, and every governed door fetches the keys it checks each token against from it: over http, whoever answers that address on the path chooses them. Name the IdP's https issuer." $issuer) -}}
{{- end -}}
{{- $issuer -}}
{{- end -}}

{{- define "lance.oidcIdpIdFormat" -}}
{{- $setting := index . 0 -}}{{- $idp := index . 1 -}}
{{- if or (not (kindIs "string" $idp)) (has $idp (list "kubernetes" "dex-client")) (not (regexMatch "^[a-z0-9-]+$" $idp)) -}}
{{- fail (printf "%s=%v is not an idp id: lower-case letters, digits and '-', never kubernetes or dex-client, the machine identities'" $setting $idp) -}}
{{- end -}}
{{- end -}}

{{- define "lance.oidcIdpId" -}}
{{- $idp := .Values.auth.oidc.idpId | default "" -}}
{{- if $idp -}}
{{- include "lance.oidcIdpIdFormat" (list "auth.oidc.idpId" $idp) -}}
{{- if .Values.auth.oidc.connectors -}}
{{- fail "auth.oidc.idpId and auth.oidc.connectors each name every person: set idpId for any OpenID provider, or connectors for a Dex that brokers several, not both" -}}
{{- end -}}
{{- end -}}
{{- $idp -}}
{{- end -}}

{{- define "lance.oidcConnectors" -}}
{{- $connectors := deepCopy (.Values.auth.oidc.connectors | default dict) -}}
{{- $idp := include "lance.oidcIdpId" . -}}
{{- if and .Values.dex.enabled (not $idp) (not (hasKey $connectors "local")) -}}
{{- $_ := set $connectors "local" "dex" -}}
{{- end -}}
{{- if and (not $connectors) (not $idp) -}}
{{- fail "auth.oidc.idpId must name the idp id of the IdP's people (e.g. corp): a person is the principal <idp-id>~<subject> the issuer names. A Dex that brokers several upstream providers may name auth.oidc.connectors instead (e.g. {ldap: corp})" -}}
{{- end -}}
{{- range $connector, $idp := $connectors -}}
{{- if not $connector -}}
{{- fail "auth.oidc.connectors names an empty connector id: Dex names every connector" -}}
{{- end -}}
{{- include "lance.oidcIdpIdFormat" (list (printf "auth.oidc.connectors.%s" $connector) $idp) -}}
{{- end -}}
{{- if $connectors -}}{{- toJson $connectors -}}{{- end -}}
{{- end -}}

{{- define "lance.oidcSubjectClaims" -}}
{{- $claims := .Values.auth.oidc.subjectClaims | default list -}}
{{- if $claims -}}
{{- if include "lance.oidcConnectors" . -}}
{{- fail "auth.oidc.subjectClaims is read only with auth.oidc.idpId: a Dex connector reports its user id in federated_claims" -}}
{{- end -}}
{{- range $claims -}}
{{- if or (not (kindIs "string" .)) (not .) -}}
{{- fail (printf "auth.oidc.subjectClaims names %v, which is no claim" .) -}}
{{- end -}}
{{- end -}}
{{- toJson $claims -}}
{{- end -}}
{{- end -}}

{{- define "lance.bootstrapAdmin" -}}
{{- $admin := .Values.auth.bootstrapAdmin | default "" | toString -}}
{{- if and $admin (or (hasPrefix "kubernetes~" $admin) (hasPrefix "dex-client~" $admin) (not (regexMatch "^[a-z0-9-]+~[^[:cntrl:]]+$" $admin))) -}}
{{- fail (printf "auth.bootstrapAdmin=%s is not a person's principal: name them <idp-id>~<subject>, auth.oidc.idpId and the subject the issuer names them by, or the idp id auth.oidc.connectors gives their Dex connector and the user id it reports (dex~<userID> for a development Dex user)" $admin) -}}
{{- end -}}
{{- $admin -}}
{{- end -}}

{{- define "lance.declaration" -}}
{{- $admin := include "lance.bootstrapAdmin" . -}}
{{- $projects := list -}}
{{- range .Values.catalog.declared.projects -}}
{{- $project := deepCopy . -}}
{{- if and (not $project.admins) $admin -}}{{- $_ := set $project "admins" (list $admin) -}}{{- end -}}
{{- $projects = append $projects $project -}}
{{- end -}}
{{- $roles := list -}}
{{- range $principal, $relations := (include "lance.fgaEstateRoles" . | fromJson) -}}
{{- range $relations -}}{{- $roles = append $roles (dict "relation" . "principal" $principal) -}}{{- end -}}
{{- end -}}
{{- $estate := dict "admins" (ternary (list $admin) list (ne $admin "")) "roles" $roles -}}
{{- dict "projects" $projects "warehouses" (.Values.catalog.declared.warehouses | default list) "grants" (.Values.catalog.declared.grants | default list) "estate" $estate | toJson -}}
{{- end -}}

{{- define "lance.oidcAudience" -}}
{{- $audience := required "auth.oidc.audience names the lakehouse's own audience, which every governed door checks" .Values.auth.oidc.audience -}}
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
{{- with include "lance.oidcIdpId" $root }}
- { name: RASK_OIDC_IDP_ID, value: {{ . | quote }} }
{{- end }}
{{- with include "lance.oidcSubjectClaims" $root }}
- { name: RASK_OIDC_SUBJECT_CLAIMS, value: {{ . | quote }} }
{{- end }}
{{- with include "lance.oidcConnectors" $root }}
- { name: RASK_OIDC_CONNECTORS, value: {{ . | quote }} }
{{- end }}
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
catalog
{{- end -}}

{{- define "lance.isFgaWriter" -}}
{{- if has (index . 1) (include "lance.fgaWriters" (index . 0) | splitList " ") }}true{{ end -}}
{{- end -}}

{{- define "lance.openfgaClients" -}}
{{- list "catalog" "hooks" | sortAlpha | join "," -}}
{{- end -}}

{{- define "lance.openfgaSubjects" -}}
{{- $v := .Values -}}{{- $p := printf "system:serviceaccount:%s:" .Release.Namespace -}}{{- $sa := printf "%s%s-sa-" $p (include "lance.fullname" .) -}}
{{- $out := list -}}
{{- if $v.auth.enabled -}}
{{- range splitList "," (include "lance.openfgaClients" .) }}{{ $out = append $out (printf "%s%s" $sa .) }}{{ end -}}
{{- end -}}
{{- $out | uniq | sortAlpha | join "," -}}
{{- end -}}

{{- define "lance.fgaEstateRoles" -}}
{{- $out := dict -}}
{{- if .Values.auth.enabled -}}
{{- $_ := set $out (include "lance.saSubject" (list . "lineage")) (list "decider" "governance_prober") -}}
{{- if .Values.maintenance.enabled }}{{- $_ := set $out (include "lance.saSubject" (list . "maintenance")) (list "reclaimer" "reconciler") -}}{{- end -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{- define "lance.ownAccounts" -}}
{{- $root := . -}}{{- $out := list -}}
{{- $console := include "lance.saName" (list $root "console") -}}
{{- range (include "lance.serviceAccounts" . | fromJsonArray) }}{{- if ne . $console }}{{- $out = append $out (include "lance.subjectOf" (list $root .)) }}{{- end }}{{- end -}}
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

{{- define "lance.bootstrapJobDeadline" -}}
activeDeadlineSeconds: 1800
{{- end -}}

{{- define "lance.objectStoreAudience" -}}
{{- required "objectStore.identity.audience names the audience every lakehouse workload's store token is projected for, which the store trusts as its identity provider's client; no workload holds a store key (ADR 0170)" .Values.objectStore.identity.audience -}}
{{- end -}}

{{- define "lance.secretNamePattern" -}}^[a-z0-9][a-z0-9-]{0,252}${{- end -}}

{{- define "lance.storeReads" -}}
{{- $v := .Values -}}{{- $root := . -}}
{{- $reads := dict -}}
{{- with $v.services.lineage.forward.authSecret.name }}{{- if not (regexMatch (include "lance.secretNamePattern" $root) (toString .)) }}{{- fail (printf "services.lineage.forward.authSecret.name: %q is no secret name (a path or a wildcard would widen lineage's store policy)" (toString .)) }}{{- end }}{{- end -}}
{{- $sa := printf "%s-sa-" (include "lance.fullname" .) -}}
{{- $doors := ne (($v.signing).doors | toString) "off" -}}
{{- $everySigner := list -}}
{{- range (include "lance.signingKeys" (list .) | splitList " " | compact) }}{{- $everySigner = append $everySigner (printf "signing-public-%s" .) }}{{- end -}}
{{- $own := dict -}}
{{- range (include "lance.signers" . | fromJsonArray) }}{{- $_ := set $own .app (concat (get $own .app | default list) (list (printf "signing-key-%s" .key) (printf "signing-public-%s" .key))) }}{{- end -}}
{{- $apps := list
  (dict "app" $v.services.catalog.appId "sa" "catalog" "api" (concat (list "commit-id-key") (ternary $everySigner list (and $v.auth.enabled $doors $v.catalog.controlEmit))))
  (dict "app" $v.services.lineage.appId "sa" "lineage" "api" (concat (compact (list $v.services.lineage.forward.authSecret.name)) (ternary $everySigner list (and $v.auth.enabled ($v.signing).enforce)))) -}}
{{- if $v.maintenance.enabled -}}
{{- $apps = append $apps (dict "app" $v.maintenance.appId "sa" "maintenance" "api" (ternary $everySigner list (and $v.auth.enabled $doors $v.maintenance.arrivalEnabled))) -}}
{{- end -}}
{{- range $apps -}}
{{- $api := concat .api (get $own .app | default list) | compact | uniq | sortAlpha -}}
{{- if $api -}}
{{- $_ := set $reads .app (dict "sa" (printf "%s%s" $sa .sa) "api" $api) -}}
{{- end -}}
{{- end -}}
{{- $held := concat (include "lance.esoReads" . | fromJsonArray) (list "nats-root" "nats-route" "nats-server" "nats-callout" "openbao-seeded" "commit-id-key" "otel-collector-export" "object-store-root" "postgres") -}}
{{- range $reads }}{{- $held = concat $held .api }}{{- end -}}
{{- $lakehouseApps := list $v.services.catalog.appId $v.services.lineage.appId $v.maintenance.appId -}}
{{- $external := $v.openbao.externalReaders | default dict -}}
{{- $sessions := include "lance.natsSessions" . | fromJson -}}
{{- range $app, $reader := $external -}}
{{- if not (regexMatch "^[a-z][a-z0-9-]{0,62}$" $app) }}{{- fail (printf "openbao.externalReaders: %q is no app id" $app) }}{{- end -}}
{{- if has $app $lakehouseApps }}{{- fail (printf "openbao.externalReaders: %q is the lakehouse's own app" $app) }}{{- end -}}
{{- $own := printf "%s-" $app -}}
{{- $claims := list -}}
{{- range $held }}{{- if or (hasPrefix $own .) (eq . (printf "nats-user-%s" $app)) }}{{- $claims = append $claims . }}{{- end }}{{- end -}}
{{- range $other, $_ := $external }}{{- if and (ne $other $app) (hasPrefix $own $other) }}{{- $claims = append $claims (printf "%s's names" $other) }}{{- end }}{{- end -}}
{{- range $family := list "nats-user-" "signing-key-" "signing-public-" }}{{- if or (hasPrefix $own $family) (hasPrefix $family $own) }}{{- fail (printf "openbao.externalReaders: %q is a reserved app id: its own names (%s<name>) would cover the store's %s<name> secrets" $app $own $family) }}{{- end }}{{- end -}}
{{- if $claims }}{{- fail (printf "openbao.externalReaders: %q is a reserved app id: its own names would cover %s" $app (join ", " ($claims | uniq))) }}{{- end -}}
{{- if eq (include "lance.storeRole" (list $root $app)) $v.externalSecrets.role }}{{- fail (printf "openbao.externalReaders: %q would log in as %s, the External Secrets Operator's own role (externalSecrets.role)" $app $v.externalSecrets.role) }}{{- end -}}
{{- $account := ($reader).serviceAccount | default "" | toString -}}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]{0,251}[a-z0-9])?$" $account) }}{{- fail (printf "openbao.externalReaders.%s.serviceAccount names no ServiceAccount" $app) }}{{- end -}}
{{- if has $account (append (include "lance.serviceAccounts" $root | fromJsonArray) "default") }}{{- fail (printf "openbao.externalReaders.%s.serviceAccount: %q is the lakehouse's own ServiceAccount or the namespace's shared default: a reader's role binds an identity of its own" $app $account) }}{{- end -}}
{{- if not ($reader).secrets }}{{- fail (printf "openbao.externalReaders.%s.secrets names no secret: a reader with no read is no reader" $app) }}{{- end -}}
{{- range $reader.secrets }}
{{- if not (regexMatch (include "lance.secretNamePattern" $root) (toString .)) }}{{- fail (printf "openbao.externalReaders.%s.secrets: %q is no secret name (a path or a wildcard would widen the store policy)" $app (toString .)) }}{{- end -}}
{{- if not (or (eq . (printf "nats-user-%s" $app)) (hasPrefix $own .) (hasPrefix "signing-public-" .)) }}{{- fail (printf "openbao.externalReaders.%s.secrets: %q is not the reader's own: a reader holds only nats-user-%s, names of its own (%s<name>) and signing-public-<name>" $app . $app $own) }}{{- end -}}
{{- if and (eq . (printf "nats-user-%s" $app)) (hasKey $sessions $app) }}{{- fail (printf "openbao.externalReaders.%s.secrets: %s is a session user, which presents its ServiceAccount token to the bus and holds no nats-user-%s" $app $app $app) }}{{- end -}}
{{- end -}}
{{- $_ := set $reads $app (dict "sa" $reader.serviceAccount "api" ($reader.secrets | uniq | sortAlpha)) -}}
{{- end -}}
{{- toJson $reads -}}
{{- end -}}

{{- define "lance.esoReads" -}}
{{- $v := .Values -}}
{{- $reads := list "postgres" -}}
{{- if $v.objectStore.enabled }}{{- $reads = append $reads "object-store-root" }}{{- end -}}
{{- if $v.observability.enabled }}{{- $reads = append $reads "otel-collector-export" }}{{- end -}}
{{- range $files := include "lance.natsFiles" . | fromJson }}{{- $reads = concat $reads $files }}{{- end -}}
{{- toJson ($reads | uniq | sortAlpha) -}}
{{- end -}}

{{- define "lance.storeRole" -}}
{{- printf "%s-%s" (include "lance.fullname" (index . 0)) (index . 1) -}}
{{- end -}}

{{- define "lance.storePolicy" -}}
{{- $mount := (index . 0).Values.externalSecrets.kvMount -}}
{{- $paths := dict "auth/token/lookup-self" (dict "capabilities" (list "read")) -}}
{{- if index . 2 }}{{- $_ := set $paths "auth/token/renew-self" (dict "capabilities" (list "update")) }}{{- end -}}
{{- range (index . 1) }}{{- $_ := set $paths (printf "%s/data/%s" $mount .) (dict "capabilities" (list "read")) }}{{- end -}}
{{- toJson (dict "path" $paths) -}}
{{- end -}}

{{- define "lance.storeIdentity" -}}
{{- $root := index . 0 -}}{{- $appId := index . 1 -}}{{- $part := index . 2 -}}
{{- if hasKey (include "lance.storeReads" $root | fromJson) $appId -}}
{{- $ca := include "lance.storeCaConfigMap" $root -}}
{{- if eq $part "mounts" }}
- { name: openbao-identity, mountPath: {{ dir (include "lance.storeTokenFile" $root) }}, readOnly: true }
{{- if $ca }}
- { name: secret-store-ca, mountPath: {{ dir (include "lance.storeCaFile" $root) }}, readOnly: true }
{{- end }}
{{- else }}
- name: openbao-identity
  projected:
    sources:
      - serviceAccountToken: { path: {{ base (include "lance.storeTokenFile" $root) }}, audience: {{ include "lance.storeAudience" $root | quote }}, expirationSeconds: 600 }
{{- if $ca }}
- name: secret-store-ca
  configMap:
    name: {{ $ca | quote }}
    items: [{ key: {{ $root.Values.openbao.ca.key | quote }}, path: {{ base (include "lance.storeCaFile" $root) }} }]
{{- end }}
{{- end }}
{{- end -}}
{{- end -}}

{{- define "lance.storeTokenSeconds" -}}
{{- $name := index . 0 -}}{{- $value := index . 1 | toString -}}
{{- if not (regexMatch "^(0|[1-9][0-9]{0,4})[smh]?$" $value) }}{{- fail (printf "openbao.identity.%s is %q: write it as <n>s, <n>m or <n>h, a number of five digits at most with no leading zero, the forms this chart reads as the store does and can hold to the hour a store token lives at most" $name $value) }}{{- end -}}
{{- $seconds := mul (regexFind "^[0-9]+" $value | int) (get (dict "s" 1 "m" 60 "h" 3600) (regexFind "[smh]$" $value | default "s")) -}}
{{- if lt $seconds 1 }}{{- fail (printf "openbao.identity.%s is %q: a role with no lifetime of its own issues tokens at the store's default, which is weeks" $name $value) }}{{- end -}}
{{- if lt $seconds 60 }}{{- fail (printf "openbao.identity.%s is %q: a store token lives at least a minute, the least that leaves a service's loop a pass to log in again before the token is used up, and a service refuses a login with a shorter lease" $name $value) }}{{- end -}}
{{- if gt $seconds 3600 }}{{- fail (printf "openbao.identity.%s is %q: a store token lives at most an hour (ADR 0169), and a service refuses a login with a longer lease" $name $value) }}{{- end -}}
{{- $seconds -}}
{{- end -}}

{{- define "lance.storeTokenFile" -}}/var/run/secrets/rask/openbao/token{{- end -}}

{{- define "lance.storeCaFile" -}}/etc/rask/secret-store-ca/ca.crt{{- end -}}

{{- define "lance.storeCaConfigMap" -}}
{{- $ca := .Values.openbao.ca | default dict -}}
{{- if $ca.configMap -}}
{{- if .Values.openbao.enabled }}{{- fail "openbao.ca names the CA of the platform's secret store, and openbao.enabled reads the development store, which serves plain HTTP inside the namespace: unset one of them" }}{{- end -}}
{{- if not $ca.key }}{{- fail "openbao.ca.key names the key of the ConfigMap openbao.ca.configMap that holds the CA certificate" }}{{- end -}}
{{- $ca.configMap -}}
{{- end -}}
{{- end -}}

{{- define "lance.secretStoreEnv" -}}
{{- $root := index . 0 -}}{{- $appId := index . 1 -}}
{{- if hasKey (include "lance.storeReads" $root | fromJson) $appId -}}
- { name: RASK_SECRET_STORE_ADDR, value: {{ include "lance.vaultAddr" $root | quote }} }
- { name: RASK_SECRET_STORE_ROLE, value: {{ include "lance.storeRole" (list $root $appId) | quote }} }
- { name: RASK_SECRET_STORE_AUTH_MOUNT, value: {{ $root.Values.externalSecrets.kubernetesAuthPath | quote }} }
- { name: RASK_SECRET_STORE_KV_MOUNT, value: {{ $root.Values.externalSecrets.kvMount | quote }} }
- { name: RASK_SECRET_STORE_TOKEN_FILE, value: {{ include "lance.storeTokenFile" $root }} }
{{- if include "lance.storeCaConfigMap" $root }}
- { name: RASK_SECRET_STORE_CA_FILE, value: {{ include "lance.storeCaFile" $root }} }
{{- end }}
{{- end -}}
{{- end -}}

{{- define "lance.storeAudience" -}}
{{- required "openbao.identity.audience names the audience each pod's store token is projected for; a role bound to none admits any token its ServiceAccount holds" .Values.openbao.identity.audience -}}
{{- end -}}

{{- define "lance.secretScopes" -}}
{{- keys (include "lance.storeReads" . | fromJson) | sortAlpha | join "," -}}
{{- end -}}

{{- define "lance.secretReaderComponents" -}}
{{- $components := list -}}
{{- range (splitList "," (include "lance.secretScopes" .) | compact) -}}
{{- if not (and $.Values.maintenance.enabled (eq . $.Values.maintenance.appId)) }}{{- $components = append $components . }}
{{- else }}{{- $components = append $components "maintenance-worker" }}{{- end -}}
{{- end -}}
{{- $components | uniq | sortAlpha | join "," -}}
{{- end -}}

{{- define "lance.allocatorEnv" -}}
{{- $root := index . 0 -}}
- { name: MALLOC_ARENA_MAX, value: {{ $root.Values.allocator.arenaMax | quote }} }
- { name: ARROW_DEFAULT_MEMORY_POOL, value: {{ $root.Values.allocator.arrowMemoryPool | quote }} }
{{- end -}}

{{- define "lance.fileAuditEnv" -}}
{{- $root := index . 0 -}}
{{- with $root.Values.observability.lanceLog }}
- { name: LANCE_LOG, value: {{ . | quote }} }
{{- end -}}
{{- end -}}

{{- define "lance.signers" -}}
{{- $v := .Values -}}{{- $s := list (dict "id" (include "lance.saSubject" (list . "catalog")) "key" (printf "%s-sa-catalog" (include "lance.fullname" .)) "app" $v.services.catalog.appId "role" "catalog") -}}
{{- if $v.maintenance.enabled }}{{- $s = append $s (dict "id" (include "lance.saSubject" (list . "maintenance")) "key" (printf "%s-sa-maintenance" (include "lance.fullname" .)) "app" $v.maintenance.appId "role" "maintenance") }}{{- end -}}
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
{{- if not (regexMatch "^[a-z0-9][a-z0-9-]*$" .key) }}{{- fail (printf "signing key name %q must match ^[a-z0-9][a-z0-9-]*$: it becomes a shell word and an OpenBao path" .key) }}{{- end }}
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
{{ .Values.vending.externalBlobBases }}
{{- end }}

{{- define "rask.catalogPublicPrefix" -}}/api/catalog{{- end -}}

{{- define "rask.lineageDatasetNamespace" -}}
{{- $host := first .Values.route.hostnames | default (printf "%s-catalog.%s" (include "lance.fullname" .) .Release.Namespace) -}}
{{- printf "lance-namespace://%s%s" $host (include "rask.catalogPublicPrefix" .) -}}
{{- end -}}

{{- define "rask.apiRoutes" -}}
{{- $root := . -}}
{{- $rows := list -}}
{{- $docs := ternary (list "/docs" "/redoc" "/openapi.json") (list) .Values.docs.enabled -}}
{{- $rows = append $rows (dict "prefix" (include "rask.catalogPublicPrefix" $root) "component" "catalog" "service" (printf "%s-catalog" (include "lance.fullname" $root)) "port" .Values.services.catalog.port "rewrite" false "paths" (concat (list "/v1" "/management/v1") $docs)) -}}
{{- $rows = append $rows (dict "prefix" "/api/lineage" "component" "lineage" "service" (printf "%s-lineage" (include "lance.fullname" $root)) "port" .Values.services.lineage.port "rewrite" true "paths" (concat (list "/events" "/api/v1/lineage" "/admin/dlq" "/tables") $docs)) -}}
{{- toJson $rows -}}
{{- end -}}
