# chart/templates/ray-auth-token.yaml

Rationale for `chart/templates/ray-auth-token.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if not .Values.ray.auth.enabled }}`

Prod signal (values-prod flips devMode): an unauthenticated Ray dashboard is remote code
execution (the jobs + Serve REST APIs take arbitrary entrypoints/import_paths), so a prod
render that deploys Ray MUST have token auth on.
The kuberay >= 1.6.0 prerequisite is SATISFIED (Chart.yaml pins 1.6.2, spec.authOptions
wired in rayservice.yaml), so values-prod ships ray.auth.enabled=true.
