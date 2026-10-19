{{- define "annotator.name" -}}{{ printf "%s-annotator" .Values.rask.release }}{{- end -}}

{{- define "annotator.selector" -}}
app.kubernetes.io/name: rask
app.kubernetes.io/instance: {{ .Values.rask.release }}
app.kubernetes.io/component: annotator
{{- end -}}

{{- define "annotator.stateCluster" -}}{{ printf "%s-state" (include "annotator.name" .) }}{{- end -}}

{{- define "annotator.secretStore" -}}{{ printf "%s-kubernetes" (include "annotator.name" .) }}{{- end -}}

{{- define "annotator.otlpEndpoint" -}}{{ .Values.otel.endpoint | default (printf "http://%s-otel-collector:4318" .Values.rask.release) }}{{- end -}}

{{- define "annotator.daprSpec" -}}
features:
  - name: HotReload
    enabled: false
{{- if .Values.otel.enabled }}
{{- $endpoint := include "annotator.otlpEndpoint" . }}
tracing:
  samplingRate: {{ .Values.otel.samplingRate | quote }}
  otel:
    endpointAddress: {{ regexReplaceAll "^https?://" $endpoint "" | quote }}
    isSecure: {{ hasPrefix "https://" $endpoint }}
    protocol: http
    timeout: "30s"
{{- end }}
metrics:
  enabled: true
  recordErrorCodes: true
  http:
    increasedCardinality: false
    pathMatching:
      - "/v1.0/actors/{actorType}/{actorId}/method/{method}"
logging:
  apiLogging:
    enabled: true
    obfuscateURLs: true
    omitHealthChecks: true
secrets:
  scopes:
    - storeName: {{ include "annotator.secretStore" . }}
      defaultAccess: deny
{{- end -}}

{{- define "annotator.daprConfigName" -}}
{{- printf "%s-%s" (include "annotator.name" .) (include "annotator.daprSpec" . | sha256sum | trunc 10) -}}
{{- end -}}
