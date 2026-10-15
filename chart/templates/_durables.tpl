{{- define "lance.chartDurables" -}}

{{- $names := list (printf "%s-durable" .Values.services.lineage.daprAppId) -}}

{{- $names = append $names "notifications-durable" -}}
{{- if and .Values.dapr.enabled .Values.services.notifications -}}
  {{- $names = append $names "notifications-control-durable" -}}
{{- end -}}

{{- if and .Values.maintenance.enabled .Values.maintenance.arrivalEnabled -}}
  {{- $names = append $names (printf "%s-durable" .Values.maintenance.daprAppId) -}}
{{- end -}}
{{- if .Values.dapr.resiliency.enabled -}}
  {{- $names = append $names (printf "%s-dlq-durable" .Values.services.lineage.daprAppId) -}}
{{- end -}}
{{- join " " (uniq $names) -}}
{{- end -}}
