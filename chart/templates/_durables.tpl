{{- define "lance.chartDurables" -}}

{{- $names := list (printf "%s-durable" .Values.services.lineage.daprAppId) -}}
{{- if .Values.medallion.enabled -}}

  {{- $names = append $names (printf "%s-durable" .Values.medallion.producer.daprAppId) -}}
  {{- range .Values.medallion.stageRunners -}}
    {{- $names = append $names (printf "%s-durable" .daprAppId) -}}
  {{- end -}}
  {{- $names = append $names (printf "%s-control-durable" .Values.medallion.producer.daprAppId) -}}
{{- end -}}

{{- $names = append $names "notifications-durable" -}}
{{- if and .Values.dapr.enabled .Values.services.notifications -}}
  {{- $names = append $names "notifications-control-durable" -}}
{{- end -}}

{{- if and .Values.maintenance.enabled .Values.maintenance.workTopic -}}
  {{- $names = append $names (printf "%s-durable" .Values.maintenance.daprAppId) -}}
{{- end -}}
{{- if .Values.dapr.resiliency.enabled -}}
  {{- $names = append $names (printf "%s-dlq-durable" .Values.services.lineage.daprAppId) -}}
{{- end -}}
{{- if and .Values.dapr.enabled .Values.maintenance.enabled .Values.maintenance.workTopic -}}
  {{- $names = append $names (printf "%s-work-durable" .Values.maintenance.daprAppId) -}}
{{- end -}}

{{- if and .Values.dapr.enabled .Values.maintenance.enabled .Values.maintenance.indexTopic -}}
  {{- $names = append $names (printf "%s-index-durable" .Values.maintenance.daprAppId) -}}
{{- end -}}
{{- join " " (uniq $names) -}}
{{- end -}}
