{{- define "console.web" -}}{{ printf "%s-console" .Values.rask.release }}{{- end -}}

{{- define "console.inbox" -}}{{ printf "%s-%s" .Values.rask.release .Values.dapr.appId }}{{- end -}}

{{- define "console.webSelector" -}}
app.kubernetes.io/name: rask
app.kubernetes.io/instance: {{ .Values.rask.release }}
app.kubernetes.io/component: console
{{- end -}}

{{- define "console.inboxSelector" -}}
app.kubernetes.io/name: rask
app.kubernetes.io/instance: {{ .Values.rask.release }}
app.kubernetes.io/component: {{ .Values.dapr.appId }}
{{- end -}}

{{- define "console.inboxSa" -}}{{ printf "%s-sa-%s" .Values.rask.release .Values.dapr.appId }}{{- end -}}

{{- define "console.stateCluster" -}}{{ printf "%s-state" (include "console.inbox" .) }}{{- end -}}

{{- define "console.kubeSecretStore" -}}{{ printf "%s-kubernetes" (include "console.inbox" .) }}{{- end -}}

{{- define "console.stateStore" -}}{{ printf "%s-statestore" (include "console.inbox" .) }}{{- end -}}

{{- define "console.lineagePubsub" -}}{{ printf "%s-lineage" (include "console.inbox" .) }}{{- end -}}

{{- define "console.controlPubsub" -}}{{ printf "%s-control" (include "console.inbox" .) }}{{- end -}}

{{- define "console.cron" -}}{{ printf "%s-reconcile-cron" (include "console.inbox" .) }}{{- end -}}

{{- define "console.email" -}}{{ printf "%s-email" (include "console.inbox" .) }}{{- end -}}

{{- define "console.natsUrl" -}}{{ .Values.bus.url | default (printf "nats://%s-nats:4222" .Values.rask.release) }}{{- end -}}

{{- define "console.otlpEndpoint" -}}{{ .Values.otel.endpoint | default (printf "http://%s-otel-collector:4318" .Values.rask.release) }}{{- end -}}

{{- define "console.natsCreds" -}}
- { name: jwt, secretKeyRef: { name: {{ printf "nats-user-%s" .Values.dapr.appId }}, key: jwt } }
- { name: seedKey, secretKeyRef: { name: {{ printf "nats-user-%s" .Values.dapr.appId }}, key: seed } }
{{- end -}}

{{- define "console.brokerWindows" -}}
{{- if .Values.bus.resiliency -}}
- { name: ackWait, value: "720s" }
- { name: maxDeliver, value: "3" }
- { name: backOff, value: "720s,720s" }
{{- else -}}
- { name: ackWait, value: "30s" }
- { name: maxDeliver, value: "5" }
- { name: backOff, value: "30s,60s,120s,300s" }
{{- end -}}
{{- end -}}

{{- define "console.daprSpec" -}}
features:
  - name: HotReload
    enabled: false
{{- if .Values.otel.enabled }}
{{- $endpoint := include "console.otlpEndpoint" . }}
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
      - "/v1.0/state/{storeName}"
      - "/v1.0/publish/{pubsubName}/{topic...}"
      - "/v1.0/actors/{actorType}/{actorId}/method/{method}"
logging:
  apiLogging:
    enabled: true
    obfuscateURLs: true
    omitHealthChecks: true
secrets:
  scopes:
    - storeName: {{ include "console.kubeSecretStore" . }}
      defaultAccess: deny
    - storeName: {{ .Values.secretStore.name }}
      defaultAccess: deny
      {{- with .Values.auth.signatures.publicKeys }}
      allowedSecrets:
        {{- range . }}
        - {{ . }}
        {{- end }}
      {{- end }}
{{- end -}}

{{- define "console.daprConfigName" -}}
{{- printf "%s-%s" (include "console.inbox" .) (include "console.daprSpec" . | sha256sum | trunc 10) -}}
{{- end -}}

{{- define "console.containerSecurity" -}}
securityContext:
  runAsNonRoot: true
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
{{- end -}}
