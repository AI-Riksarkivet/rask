# chart/templates/catalog-erasure-release-cron.yaml

Rationale for `chart/templates/catalog-erasure-release-cron.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.dapr.enabled .Values.dapr.sidecars }}`

ADR 0138: releases an erasure's pinning tags and branches when due. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string.
