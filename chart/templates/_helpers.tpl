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

{{/* Postgres password: pinned across upgrades via lookup, else random. */}}
{{- define "rask.pgPassword" -}}
{{- if .Values.secrets.postgresPassword -}}
{{- .Values.secrets.postgresPassword -}}
{{- else -}}
{{- $existing := (lookup "v1" "Secret" .Release.Namespace (printf "%s-postgres" (include "rask.fullname" .))) -}}
{{- if and $existing $existing.data (index $existing.data "password") -}}
{{- index $existing.data "password" | b64dec -}}
{{- else -}}
{{- randAlphaNum 24 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "rask.minioAccessKey" -}}
{{- default "raskadmin" .Values.secrets.minioAccessKey -}}
{{- end -}}

{{- define "rask.minioSecretKey" -}}
{{- if .Values.secrets.minioSecretKey -}}
{{- .Values.secrets.minioSecretKey -}}
{{- else -}}
{{- $existing := (lookup "v1" "Secret" .Release.Namespace (printf "%s-rustfs" (include "rask.fullname" .))) -}}
{{- if and $existing $existing.data (index $existing.data "secretkey") -}}
{{- index $existing.data "secretkey" | b64dec -}}
{{- else -}}
{{- randAlphaNum 24 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* asyncpg DATABASE_URL pointing at the in-cluster postgres service. */}}
{{- define "rask.databaseUrl" -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgres-rw:%v/%s" .Values.cnpg.user (include "rask.pgPassword" .) (include "rask.fullname" .) .Values.cnpg.port .Values.cnpg.database -}}
{{- end -}}

{{/* Dapr sidecar pod annotations (no-op unless dapr.sidecars). Shared by fleet +
     controlplane so the annotation set never drifts.
     Usage: {{- include "rask.daprAnnotations" (list $root $appId $appPort) | nindent 8 }} */}}
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
{{- end }}
{{- end -}}

{{/* OTLP/OpenTelemetry container env (no-op unless observability.enabled). Shared by
     fleet + controlplane so the OTLP wiring never drifts. The GreptimeDB host + the
     metrics/traces header split live here, in one place.
     Usage: {{- include "rask.otelEnv" (list $root "service-name") | nindent 12 }} */}}
{{- define "rask.otelEnv" -}}
{{- $root := index . 0 -}}
{{- $svc := index . 1 -}}
{{- if $root.Values.observability.enabled -}}
- name: RASK_OTEL_ENABLED
  value: "true"
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: "http://rask-greptimedb-standalone:4000/v1/otlp"
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: "http/protobuf"
# Generic headers apply to metrics (and anything without a signal-specific
# override): db-name only — GreptimeDB ingests OTLP metrics with no pipeline.
- name: OTEL_EXPORTER_OTLP_HEADERS
  value: "x-greptime-db-name=public"
# Traces additionally need GreptimeDB's trace pipeline; signal-specific
# headers override the generic ones for traces only.
- name: OTEL_EXPORTER_OTLP_TRACES_HEADERS
  value: "x-greptime-db-name=public,x-greptime-pipeline-name=greptime_trace_v1"
- name: OTEL_SERVICE_NAME
  value: {{ $svc | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "service.namespace=rask,deployment.environment=%s" $root.Release.Namespace | quote }}
{{- end }}
{{- end -}}
