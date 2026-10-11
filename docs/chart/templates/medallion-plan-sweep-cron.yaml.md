# chart/templates/medallion-plan-sweep-cron.yaml

Rationale for `chart/templates/medallion-plan-sweep-cron.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.dapr.enabled .Values.medallion.enabled .Values.medallion.ray .Valu...`

THE PLAN SWEEP (CP-029 D-6): one tick per firing on every app that plans Ray runs, over that app's own plans:
the producer's training runs, and each stage runner's stage runs under medallion.compute. Each sidecar delivers to
its own app at POST /<name>, the string MEDALLION_PLAN_SWEEP_BINDING_NAME carries. No lease: plan writes are CAS.
