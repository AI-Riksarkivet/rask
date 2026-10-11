{{- define "rask.rayClusterConfig" }}
    rayVersion: {{ .Values.ray.rayVersion | quote }}
    {{- if .Values.ray.auth.enabled }}
    authOptions:
      mode: token
      secretName: {{ include "rask.fullname" . }}-ray-auth-token
    {{- end }}
    headGroupSpec:
      rayStartParams:
        dashboard-host: "0.0.0.0"
        num-gpus: "{{ .Values.ray.gpuCount }}"
        {{- if include "lance.otelEnabled" . }}
        tracing-startup-hook: "service_kit.ray_tracing:setup_tracing"
        {{- end }}
      template:
        spec:
          serviceAccountName: {{ include "lance.fullname" . }}-sa-ray

          {{- if include "rask.gpuEnabled" . }}
          runtimeClassName: {{ .Values.ray.runtimeClassName }}
          {{- end }}
          containers:
            - name: ray-head
              image: "{{ .Values.ray.image.repository }}:{{ .Values.ray.image.tag }}"
              imagePullPolicy: {{ .Values.ray.image.pullPolicy }}
              env:
                {{- include "rask.rayOtelEnv" (list $ "ray") | nindent 16 }}
                {{- if include "lance.otelEnabled" . }}
                - name: RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH
                  value: "service_kit.ray_tracing:serve_span_processors"
                - name: RAY_SERVE_TRACING_SAMPLING_RATIO
                  value: {{ .Values.ray.serveTracingSamplingRatio | default "1.0" | quote }}
                {{- end }}
                - name: RAY_LOGGING_CONFIG_ENCODING
                  value: "JSON"
                - name: RAY_SERVE_LOG_ENCODING
                  value: "JSON"
                - name: RAY_ENABLE_UV_RUN_RUNTIME_ENV
                  value: "0"
                - name: HF_HOME
                  value: /cache/hf
                {{- if .Values.secrets.hfTokenInStore }}
                - name: HF_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: {{ include "rask.fullname" . }}-hf-token
                      key: token
                {{- end }}
                - name: S3_ENDPOINT
                  value: {{ include "lance.s3Endpoint" . | quote }}
                - name: S3_REGION
                  value: "us-east-1"
                - name: S3_KEY
                  valueFrom:
                    secretKeyRef:
                      name: {{ include "lance.fullname" . }}-infra-credentials
                      key: ray-compute-access-key
                - name: S3_SECRET
                  valueFrom:
                    secretKeyRef:
                      name: {{ include "lance.fullname" . }}-infra-credentials
                      key: ray-compute-secret-key

                {{- include "lance.lineageEmitEnv" (list .) | nindent 16 }}
              ports:
                - {containerPort: 6379, name: gcs}
                - {containerPort: 8265, name: dashboard}
                - {containerPort: 10001, name: client}
                - {containerPort: 8000, name: serve}
                - {containerPort: 8080, name: metrics}
              resources:
                requests:
                  {{- toYaml .Values.ray.resources.requests | nindent 18 }}
                limits:

                  {{- if include "rask.gpuEnabled" . }}
                  nvidia.com/gpu: {{ .Values.ray.gpuCount }}
                  {{- end }}
                  {{- toYaml .Values.ray.resources.limits | nindent 18 }}
              {{- if .Values.ray.cluster.gcsFaultTolerance.enabled }}
              env:
                - {name: RAY_gcs_storage, value: rocksdb}
                - {name: RAY_gcs_storage_path, value: /var/lib/ray-gcs}
              {{- end }}
              volumeMounts:
                {{- include "lance.identityTokens" (list "mounts" "rask-lineage" "rask-medallion") | nindent 16 }}
                - {name: dshm, mountPath: /dev/shm}
                - {name: hf-cache, mountPath: /cache/hf}
                {{- if .Values.ray.cluster.gcsFaultTolerance.enabled }}
                - {name: gcs-store, mountPath: /var/lib/ray-gcs}
                {{- end }}
          volumes:
            {{- include "lance.identityTokens" (list "volumes" "rask-lineage" "rask-medallion") | nindent 12 }}
            - name: dshm
              emptyDir:
                medium: Memory
                sizeLimit: {{ .Values.ray.shmSize }}
            - name: hf-cache
              persistentVolumeClaim:
                claimName: {{ include "rask.fullname" . }}-hf-cache
            {{- if .Values.ray.cluster.gcsFaultTolerance.enabled }}
            - name: gcs-store
              persistentVolumeClaim:
                claimName: {{ include "rask.fullname" . }}-ray-gcs
            {{- end }}
    workerGroupSpecs: []
{{- end -}}
