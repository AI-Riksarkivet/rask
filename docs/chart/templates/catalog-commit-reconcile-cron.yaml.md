# chart/templates/catalog-commit-reconcile-cron.yaml

Rationale for `chart/templates/catalog-commit-reconcile-cron.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if .Values.dapr.enabled }}`

LH-497: the commit is the outbox. A write is announced after its commit, and a crash between the commit and the staging of its event loses the announcement; this cron has the catalog read each table's version log after its mark and announce every committed version lineage's feed lacks, under the run id derived from the commit and signed for the commit's author. Delivered to POST /<name> at the catalog's pod root; name, env and route are one string. Every catalog replica ticks, and an announcement two replicas make at once lands one feed row, since both carry the commit's run id.
