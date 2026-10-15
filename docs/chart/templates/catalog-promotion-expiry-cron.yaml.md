# chart/templates/catalog-promotion-expiry-cron.yaml

Rationale for `chart/templates/catalog-promotion-expiry-cron.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if .Values.dapr.enabled }}`

ADR 0156 C1 (LH-466): expires a promotion hold nobody answered before its project's `review_hours` passed, leaving `published` where it was. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks, and each hold expires once by an ETag-guarded write.
