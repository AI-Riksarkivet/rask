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
