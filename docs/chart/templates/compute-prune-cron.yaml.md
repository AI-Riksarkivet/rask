# chart/templates/compute-prune-cron.yaml

Rationale for `chart/templates/compute-prune-cron.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.dapr.enabled .Values.services.compute }}`

Job-history retention for the Ray cluster (#136). Ray's Jobs API keeps every submission
forever and its listing takes no parameters, so history grows without bound — the live
cluster reached 81,155 jobs / 164.7 MB per listing, which is what OOMKilled the compute
service. This cron binding ticks the compute service's sidecar-only prune route
(POST /compute-prune-jobs-cron, guarded by require_dapr_token), which deletes TERMINAL jobs
beyond the newest RASK_PRUNE_KEEP_JOBS (code default 500) via the spec's one reclamation
lever, DELETE /api/jobs/{id}. Mirrors the maintenance crons: schedule is component config,
scoped to the one consuming app-id, gated on dapr.enabled (no control plane, no Component CRD).
