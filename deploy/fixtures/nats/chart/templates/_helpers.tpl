{{- define "bus.labels" -}}
app.kubernetes.io/name: bus
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
rask.dev/fixture: not-for-production
{{- end -}}
