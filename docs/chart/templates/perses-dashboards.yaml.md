# chart/templates/perses-dashboards.yaml

Rationale for `chart/templates/perses-dashboards.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: v1`

Perses dashboards-as-code. Provisioned by mounting this ConfigMap into a folder listed in
perses.config.provisioning.folders (see values.yaml); Perses loads the Project + Dashboards
on startup and reconciles them every provisioning interval. Queries target the default
`greptimedb` Prometheus datasource.

ONE project ("rask"), six dashboards — the merged estate is one platform under one
release, so lance-ns's dashboards moved from the `lance-ns` project onto this one rather
than provisioning a second, near-empty project beside it:
  fleet-red    — rask's fleet RED view (OTLP http.server metrics from service_kit.setup_otel)
  lineage      — lance-ns ingest domain signal + HTTP RED
  service-flow — the event-driven medallion cascade edges + per-service activity
  training     — per-model training run metrics (Ray train job → OTLP)
  notifications— the targeted inbox: both ingress lanes, fan-out outcomes, the DLQ floor
  maintenance  — the 120s table-maintenance sweep: coverage, refusals, and what it RECLAIMED

Gated on observability.enabled ALONE, matching Chart.yaml's `condition: observability.enabled`
on the perses dependency — so the dashboards exist exactly when Perses does. (Both charts
previously wrapped this in `index .Values "perses" "enabled" | default true`, which is the
boolean form of the swallow-an-explicit-value trap: `false | default true` is true, so the
knob never worked, and no `perses.enabled` key exists to drive it.)

NB: Perses `seriesNameFormat` uses {{`{{label}}`}} syntax — escaped here so Helm emits the
literal braces instead of evaluating them as template actions.
