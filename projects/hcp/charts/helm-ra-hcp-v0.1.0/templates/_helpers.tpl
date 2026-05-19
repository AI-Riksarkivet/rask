{{/*
Expand the name of the chart.
*/}}
{{- define "ra-hcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "ra-hcp.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "ra-hcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "ra-hcp.labels" -}}
helm.sh/chart: {{ include "ra-hcp.chart" . }}
{{ include "ra-hcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "ra-hcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ra-hcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Redis fullname.
*/}}
{{- define "ra-hcp.redisFullname" -}}
{{- printf "%s-redis" (include "ra-hcp.fullname" .) }}
{{- end }}

{{/*
Redis selector labels.
*/}}
{{- define "ra-hcp.redisSelectorLabels" -}}
app.kubernetes.io/name: {{ include "ra-hcp.name" . }}-redis
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Frontend fullname.
*/}}
{{- define "ra-hcp.frontendFullname" -}}
{{- printf "%s-frontend" (include "ra-hcp.fullname" .) }}
{{- end }}

{{/*
Frontend selector labels.
*/}}
{{- define "ra-hcp.frontendSelectorLabels" -}}
app.kubernetes.io/name: {{ include "ra-hcp.name" . }}-frontend
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
