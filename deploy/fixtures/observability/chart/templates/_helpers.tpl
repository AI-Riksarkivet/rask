{{- define "observability.labels" -}}
app.kubernetes.io/name: observability
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
rask.dev/fixture: not-for-production
{{- end -}}

{{- define "observability.store" -}}http://{{ index .Values "greptimedb-standalone" "fullnameOverride" }}:{{ .Values.store.port }}{{- end -}}
