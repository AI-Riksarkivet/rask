# deploy/fixtures/observability/chart/templates/dashboards.yaml

Rationale for the observability fixture's `templates/dashboards.yaml`, kept beside the fixture rather than in its
templates. Each section is headed by the template line it explains.

## `apiVersion: v1`

Perses dashboards-as-code. Provisioned by mounting this ConfigMap into a folder listed in
perses.config.provisioning.folders (see values.yaml); Perses loads the Project + Dashboards
on startup and reconciles them every provisioning interval. Queries target the default
`greptimedb` Prometheus datasource.

ONE project ("rask"), five dashboards — the merged estate is one platform under one
release, so lance-ns's dashboards moved from the `lance-ns` project onto this one rather
than provisioning a second, near-empty project beside it:
  fleet-red    — rask's fleet RED view (OTLP http.server metrics from `opentelemetry-instrument`)
  lineage      — lance-ns ingest domain signal + HTTP RED
  service-flow — per-service activity of the event-driven services
  maintenance  — the 120s maintenance sweep: coverage, refusals, and what it RECLAIMED

Gated on observability.enabled ALONE, matching Chart.yaml's `condition: observability.enabled`
on the perses dependency — so the dashboards exist exactly when Perses does. (Both charts
previously wrapped this in `index .Values "perses" "enabled" | default true`, which is the
boolean form of the swallow-an-explicit-value trap: `false | default true` is true, so the
knob never worked, and no `perses.enabled` key exists to drive it.)

NB: Perses `seriesNameFormat` uses {{`{{label}}`}} syntax — escaped here so Helm emits the
literal braces instead of evaluating them as template actions.
