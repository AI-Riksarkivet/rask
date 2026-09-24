{{- /* THE RAY CLUSTER'S SHAPE, DEFINED ONCE.

     Two objects need it and they are different questions. A RayService wraps a cluster to run SERVE
     applications; a RayCluster is the cluster on its own, for the batch lane that submits Jobs and
     serves nothing. Rendering the spec twice would put the estate's OTel wiring, its token auth, its
     GPU runtime class and its credential plumbing in two files that drift silently — and drift here
     is invisible, because whichever object an estate does not render is never checked.

     THE INDENTATION IS 4 SPACES AND STAYS THAT WAY. It is what `rayClusterConfig:` already required,
     and YAML only demands that a mapping's keys be consistent and deeper than their parent — so the
     same block sits under a RayCluster's `spec:` unchanged. Re-indenting to 2 would have made the
     extraction a rewrite of 250 lines whose render nobody could diff.

     `$secretName` is re-derived here rather than passed, so the define is self-contained: it comes
     from `.Values` and the release name, both of which the root context already carries. */}}
{{- define "rask.rayClusterConfig" -}}
{{- $secretName := .Values.existingSecret | default (printf "%s-app" (include "rask.fullname" .)) }}
    rayVersion: {{ .Values.ray.rayVersion | quote }}
    {{- if .Values.ray.auth.enabled }}
    {{- /* Token auth (gate 7 / R3), the NATIVE kuberay >= 1.6.0 wiring: authOptions.secretName
         points the operator at the chart's pre-existing <release>-ray-auth-token Secret
         (data key `auth_token` = the operator's RAY_AUTH_TOKEN_SECRET_KEY convention, so the
         operator SKIPS generating its own Secret — raycluster_controller.reconcileAuthSecret
         returns early when secretName is set). The operator then injects RAY_AUTH_MODE=token +
         RAY_AUTH_TOKEN (secretKeyRef) into EVERY Ray container — head, workers, autoscaler —
         and, critically, the RayService controller's dashboard client reads THIS secret to
         authenticate its own /api/serve reconcile calls (utils.GetRayDashboardClientFunc);
         the old manual head-env pattern never told the controller, so it 401-wedged at
         WaitForServeDeploymentReady. Fleet consumers (compute) still get the pair via
         rask.rayAuthEnv — that half stays chart-wired. Requires rayVersion >= 2.52.0
         (operator validation); we pin {{ .Values.ray.rayVersion }}. */}}
    authOptions:
      mode: token
      secretName: {{ include "rask.fullname" . }}-ray-auth-token
    {{- end }}
    headGroupSpec:
      rayStartParams:
        dashboard-host: "0.0.0.0"
        num-gpus: "{{ .Values.ray.gpuCount }}"
        {{- if include "lance.otelEnabled" . }}
        {{- /* Ray CORE tracing. HEAD ONLY, and that is not a simplification: the hook is persisted to
             GCS internal KV by `start_head_processes()` and every Python process that connects reads
             it from there — that IS the propagation mechanism, so the same key on a workerGroupSpec
             is a silent no-op.

             The honest ceiling, so nobody over-invests: upstream documents core tracing as Alpha and
             "no longer under active development", and Ray Data / Train / Tune contribute ZERO spans
             of their own. The payoff is generic per-task spans, not dataset or operator spans. The
             job-level continuity that actually matters is already built by hand — ray_kit.trace_env
             injects TRACEPARENT and the job scripts parent on it. */}}
        tracing-startup-hook: "service_kit.ray_tracing:setup_tracing"
        {{- end }}
      template:
        spec:
          {{- /* The nvidia RuntimeClass exists only on a GPU estate (runtimeclass.yaml renders it under
               the same rask.gpuEnabled gate), and naming a RuntimeClass whose handler containerd does
               not register makes every head pod fail to create. GPU-less => no runtimeClassName at all,
               so the head runs under the node's default runtime. */}}
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
                {{- /* Ray SERVE tracing — a SEPARATE switch from the core hook above, with a DIFFERENT
                     contract: this import path must return a list of SpanProcessor (Serve builds the
                     provider), while the core hook returns None and sets it. Cross-wiring them fails
                     SOFT — Serve catches the bad import, logs "the proxy/replica will continue
                     running", and nothing goes unhealthy — so verify by observing spans in
                     `opentelemetry_traces`, never by checking health.

                     This is the higher-value half of the two: Serve honours an INBOUND traceparent,
                     so it is what joins a gateway-originated trace to the model call. Ray's own
                     monitoring docs never mention the switch exists.

                     Container env, not serveConfigV2: replicas are scheduled wherever, and every
                     future workerGroupSpecs container needs these two lines too. */}}
                - name: RAY_SERVE_TRACING_EXPORTER_IMPORT_PATH
                  value: "service_kit.ray_tracing:serve_span_processors"
                {{- /* DEFAULT IS 0.01 — one request in a hundred. A ten-request smoke test against the
                     upstream default produces zero spans and reads as "tracing is broken". */}}
                - name: RAY_SERVE_TRACING_SAMPLING_RATIO
                  value: {{ .Values.ray.serveTracingSamplingRatio | default "1.0" | quote }}
                {{- end }}
                {{- /* STRUCTURED RAY LOGS. Two separate planes again, and the Serve half is the one
                     that pays off immediately: Serve replicas default to STDERR, which the Collector's
                     filelog receiver already tails, so these logs are being ingested UNPARSED right
                     now — one body string, no severity, no job or replica id to filter on. Setting the
                     encoding makes them queryable without any shipper work.

                     The CORE half (task/actor/driver) is different: Ray writes those to files under
                     /tmp/ray inside the container, which nothing mounts and nothing tails, so JSON
                     alone does not ship them — that needs the log sidecar in open_ray_handover.md
                     section 3. Set here anyway so the records are structured the moment a shipper
                     lands, rather than needing a second rollout to become useful.

                     Must be set BEFORE `import ray`, which a container env satisfies by construction.
                     Two documented traps: the emitted key is `task_func_name`, not the
                     `task_function_name` the prose promises, and there is no `node_ip_address` field
                     (it is `node_id`). RAY_BACKEND_LOG_JSON is deliberately NOT set — it converts only
                     the Job Supervisor among the Python system components, so it buys almost nothing
                     while reading as though it structured the lot. */}}
                - name: RAY_LOGGING_CONFIG_ENCODING
                  value: "JSON"
                - name: RAY_SERVE_LOG_ENCODING
                  value: "JSON"
                {{- /* Ray-container auth env is NOT set here: spec.authOptions above makes the
                     kuberay 1.6+ operator inject RAY_AUTH_MODE/RAY_AUTH_TOKEN into every Ray
                     container itself (SetContainerTokenAuthEnvVars, skip-if-already-set), head
                     AND any future workerGroupSpecs entry — one wiring, zero drift.
                     rask.rayAuthEnv remains the FLEET-consumer half (compute). */}}
                - name: RAY_ENABLE_UV_RUN_RUNTIME_ENV
                  value: "0"
                - name: HF_HOME
                  value: /cache/hf
                - name: HF_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: {{ $secretName }}
                      key: HF_TOKEN
                {{- /* THE JOB SECRETS LIVE ON THE POD, NOT IN THE SUBMISSION (docs/DECISIONS.md "The Python estate audit"
                     P0, fixed 2026-08-28). They rode `runtime_env.env_vars`, and the Ray Jobs API
                     echoes runtime_env back on `GET /api/jobs/<id>` — an unauthenticated dashboard,
                     proxied by compute at /api/ray/*, published at the edge: one GET yielded the
                     estate's service credential. Ray merges runtime_env OVER the process env, so
                     holding them here keeps every job's `os.environ` contract byte-identical while
                     the submission body carries nothing worth echoing.
                     secretKeyRef onto Secrets the estate ALREADY owns — the same objects
                     ExternalSecrets syncs from OpenBao on the prod path. A literal value: here would
                     put the credential in `helm get manifest`, the exact class the infra-credentials
                     header documents for the Dex secret. Any future workerGroupSpecs container needs
                     these entries too (jobs run on workers). */}}
                {{- /* BOTH HALVES FROM ONE OBJECT, and that is the fix rather than tidiness. The key
                     used to arrive only through the submitter's `runtime_env` while the secret came
                     from here, so the pair had two owners: repointing the pod alone gave every job
                     `SignatureDoesNotMatch`, and repointing the stage runner alone took the STAGE RUNNER down
                     (it does its own S3 work — `outbox.stage_event` issues a HeadBucket, which needs
                     an unconditioned ListBucket a scoped policy refuses). Both measured 2026-08-30.
                     `ray-compute-*` defaults to the root credential, so this is inert until an
                     operator provisions the scoped MinIO user. NOTHING credential-shaped rides the
                     submission any more, so this pod is the single source and there is no second
                     value to keep in agreement. */}}
                {{- /* THE ADDRESS THE CREDENTIAL ABOVE IS USED AGAINST, owned by the same pod for the
                     same reason. `scripts/ray_stage_job.py:86` reads `os.environ["S3_ENDPOINT"]` with a
                     BRACKET, so a job that does not get it dies before reading a byte — and until this
                     row existed the only thing supplying it was the SUBMITTER, which injected it into
                     `runtime_env.env_vars` on every submission (measured 2026-09-18 on the deployed
                     head: `S3_KEY` and `S3_SECRET` present, `S3_ENDPOINT` absent).

                     An endpoint is NOT a secret — it is public routing information, and an inline
                     value is the right delivery for it where a `secretKeyRef` would only hide it from
                     the reader who needs it. That asymmetry is pinned by
                     `tests/unit/test_the_ray_pod_owns_every_name_its_jobs_require.py`.

                     It is also what unblocks [[LH-159]]: routing `workflow.py` through
                     `executor_for(RAY_ENGINE)` sends `WorkOrder.to_env()` and nothing else, and
                     `WorkOrder` is `extra="forbid"`, so a deployment fact cannot ride the order. */}}
                - name: S3_ENDPOINT
                  value: {{ include "lance.s3Endpoint" . | quote }}
                {{- /* The literal, matching `services.yaml:121` and both maintenance templates. A new
                     `minio.region` value would be a fourth spelling of one constant that nothing in
                     this estate varies. `test_every_chart_value_a_template_names_actually_exists`
                     does NOT catch such a key on its own: it refuses an UNGUARDED undeclared path,
                     and a `| default` supplies a value so nothing nil reaches the manifest — measured
                     2026-09-24 by rendering `.Values.minio.region | default "us-east-1"` past it.
                     The convention is what keeps the spelling from multiplying, not that gate. Inert today —
                     `scripts/ray_stage_job.py:89` defaults to the same string — and here so the pod
                     owns the whole pair rather than half of it. */}}
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
                - name: LINEAGE_SERVICE_TOKEN
                  valueFrom:
                    secretKeyRef:
{{- if .Values.auth.dedicatedServiceCredentials }}
                      {{- /* THE TRAINER'S OWN CREDENTIAL, because that is what the door demands. The
                           same flag renders `LINEAGE_PRIVILEGED_SUBJECTS` with `trainerIdentity` on it
                           (`services.yaml`), and `dapr_auth.service_principal` refuses a privileged
                           subject that presents the SHARED token — deliberately, since one token
                           across an allowlist lets any holder claim the most privileged name on it.
                           The head runs no daprd, so it cannot read `service-token-<identity>` out of
                           the secret store the way every first-party service does; it mounts it off
                           infra-credentials, the same route `ray-compute-*` above takes.

                           Handed the shared token instead, the train job logged `lineage emit attempt
                           1 rejected: HTTP 401` on every event and published its model anyway, so a
                           governed training run lost ALL of its provenance with nothing red (measured
                           2026-09-06). NOT optional here: with the flag on, a head that cannot mount
                           this can only emit refusals, and that belongs at schedule time. */}}
                      name: {{ include "lance.fullname" . }}-infra-credentials
                      key: service-token-{{ .Values.medallion.train.trainerIdentity }}
{{- else }}
                      name: {{ .Release.Name }}-dapr-app-token
                      key: token
                      {{- /* optional: the token Secret renders only with dapr.sidecars, and the jobs
                           read this with .get() — absent token = header omitted = the open (auth-off)
                           ingest, which is that profile's correct behaviour. S3_SECRET above is NOT
                           optional: a job hard-requires it, and a pod that cannot mount it should
                           fail at schedule time, not at job time. */}}
                      optional: true
{{- end }}
{{- if .Values.auth.dedicatedServiceCredentials }}
                {{- /* ONE POD, SEVERAL IDENTITIES. This head runs `ray_train_job.py` (claiming
                     `trainerIdentity`) AND every stage lane's job, which authenticates as its
                     submitting stage runner's own `fga_service_identity` — so the single
                     `LINEAGE_SERVICE_TOKEN` above can be right for exactly one of them, and the door
                     refuses a privileged subject presenting another's key with no fallback.

                     NOT FIRING, AND NOT FOR THE REASON A READER WOULD GUESS. `stage_lineage_url` IS
                     wired: measured 2026-09-18, all three stage runners carry
                     `MEDALLION_STAGE_LINEAGE_URL=http://rask-lineage:8000`. The stage lanes still emit
                     nothing from the JOB because `scripts/ray_stage_job.py` never reads `LINEAGE_URL`
                     at all — it writes the `lineage` COLUMN from `RASK_LINEAGE_DOCUMENT`, and
                     `scripts/ray_train_job.py:202` is the only reader. So wiring the URL does not arm
                     this mismatch; teaching the stage job to POST would, and that is the change that
                     must carry the per-lane token with it.
                     What the door does with a mismatch was measured directly, one POST replayed twice
                     from inside the head: `service-trainer` -> 201, a second subject -> 401 "the
                     presented credential may not claim '<subject>'", while the job writes its data and
                     exits SUCCEEDED. So the credentials land before the lane is wired, not after it
                     silently loses its provenance.

                     The job-side emitters pick `RASK_LINEAGE_TOKEN_<IDENTITY>` for the identity they
                     claim. Only the identity rides the job's `runtime_env`; a token there is the P0
                     leak `ray_submit` records, because Ray echoes runtime_env back on the job. */}}
                - name: RASK_LINEAGE_TOKEN_{{ .Values.medallion.train.trainerIdentity | upper | replace "-" "_" }}
                  valueFrom:
                    secretKeyRef:
                      name: {{ include "lance.fullname" . }}-infra-credentials
                      key: service-token-{{ .Values.medallion.train.trainerIdentity }}
                {{- range .Values.medallion.stageRunners }}
                - name: RASK_LINEAGE_TOKEN_{{ .serviceIdentity | upper | replace "-" "_" }}
                  valueFrom:
                    secretKeyRef:
                      name: {{ include "lance.fullname" $ }}-infra-credentials
                      key: service-token-{{ .serviceIdentity }}
                {{- end }}
{{- end }}
                {{- /* PRE-STAGED for P7b, and inert today — stated plainly so nobody reads it as wiring
                     that already works. This image is built solely from runners/htr's own lock
                     (.docker/ray-cluster.dockerfile), and lineage-kit is deliberately NOT a dependency
                     there, so nothing in this container can call build_emitter() at all. The env is
                     rendered now so the transport is present the day the actor seam lands, rather than
                     that day beginning with a silent NoopEmitter; wiring the emission itself is P7b's
                     job. Workers inherit the raylet's environment, and workerGroupSpecs is empty today.
                     NOTE: a P7b image that ships lineage-kit will ALSO need the service door
                     (LINEAGE_SERVICE_TOKEN + LINEAGE_SERVICE_ID) under auth.enabled — ray_submit.py
                     already threads both into the train job's runtime_env; follow that shape. */}}
                {{- include "lance.lineageEmitEnv" (list .) | nindent 16 }}
              ports:
                - {containerPort: 6379, name: gcs}
                - {containerPort: 8265, name: dashboard}
                - {containerPort: 10001, name: client}
                - {containerPort: 8000, name: serve}
                {{- /* Ray's Prometheus endpoint. KubeRay injects this containerPort itself, but the
                     Collector's `ray-pods` job keeps on the port NAME, and a relabel cannot see a port
                     the rendered pod spec does not advertise — so an operator-injected default would
                     have produced zero targets with nothing to indicate why. Declared here, the head's
                     telemetry surface is a property of this manifest rather than of the operator's
                     defaults, and it is asserted by
                     tests/unit/test_invariants.py::test_the_ray_head_declares_the_port_its_metrics_are_served_on.
                     Every future workerGroupSpecs container needs the same entry — workers serve their
                     own per-node metrics and are scraped by the same job. */}}
                - {containerPort: 8080, name: metrics}
              resources:
                requests:
                  {{- toYaml .Values.ray.resources.requests | nindent 18 }}
                limits:
                  {{- /* Omitted when GPU-less rather than rendered as `nvidia.com/gpu: 0`: an extended
                       resource the cluster never advertises has no business appearing in the pod spec —
                       it makes the manifest claim a GPU plane that does not exist, which is exactly what
                       sent the live-proof debugging at the head pod instead of at the Serve actor. */}}
                  {{- if include "rask.gpuEnabled" . }}
                  nvidia.com/gpu: {{ .Values.ray.gpuCount }}
                  {{- end }}
                  {{- toYaml .Values.ray.resources.limits | nindent 18 }}
              {{- if .Values.ray.cluster.gcsFaultTolerance.enabled }}
              {{- /* GCS FAULT TOLERANCE via the EMBEDDED RocksDB backend — the third path, and the one
                     that needs no Redis. Ray documents it ALPHA, Linux-only and SINGLE-WRITER; the
                     claim above is ReadWriteOnce for that last reason.
                     WHAT IT BUYS, honestly: it persists the GCS's CLUSTER METADATA, so a head restart
                     stops erasing the job records the cascade polls — which is what turns an UNKNOWN
                     job state into a known terminal one and lets the executor claim DURABLE_RECORD.
                     WHAT IT DOES NOT: it is not application state. On a head-only cluster the driver
                     still dies with the head; workers are what make that survivable. */}}
              env:
                - {name: RAY_gcs_storage, value: rocksdb}
                - {name: RAY_gcs_storage_path, value: /var/lib/ray-gcs}
              {{- end }}
              volumeMounts:
                - {name: dshm, mountPath: /dev/shm}
                - {name: hf-cache, mountPath: /cache/hf}
                {{- if .Values.ray.cluster.gcsFaultTolerance.enabled }}
                - {name: gcs-store, mountPath: /var/lib/ray-gcs}
                {{- end }}
          volumes:
            - name: dshm
              emptyDir:
                medium: Memory
                sizeLimit: {{ .Values.ray.shmSize }}
            - name: hf-cache
              persistentVolumeClaim:
                claimName: {{ include "rask.fullname" . }}-hf-cache
            {{- if .Values.ray.cluster.gcsFaultTolerance.enabled }}
            {{- /* A CLAIM, NOT AN EMPTYDIR, and the distinction is the whole feature: the GCS store
                   exists to outlive the pod that writes it, and an emptyDir dies with exactly that
                   pod. ReadWriteOnce is also load-bearing rather than a default — RocksDB is
                   single-writer, so the claim must not be attachable twice. */}}
            - name: gcs-store
              persistentVolumeClaim:
                claimName: {{ include "rask.fullname" . }}-ray-gcs
            {{- end }}
    workerGroupSpecs: []
{{- end -}}
