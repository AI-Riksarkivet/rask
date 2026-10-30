# chart/templates/catalog-manifest-reclaim-cron.yaml

Rationale for `chart/templates/catalog-manifest-reclaim-cron.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if .Values.dapr.enabled }}`

ADR 0185 (LH-251): removes each root's `__manifest` versions older than `catalog.manifestReclaim.retainSeconds`, which no `dir` commit reclaims. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks, and two reclaims of one root remove the same versions.
