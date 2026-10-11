# chart/templates/age-postgres.yaml

Rationale for `chart/templates/age-postgres.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $existingSts := lookup "apps/v1" "StatefulSet" .Release.Namespace (include "lance.ageHost" .) }}`

Guard: StatefulSet volumeClaimTemplates are immutable, so bumping age.storage on an upgrade
either hard-fails deep in the apply or (worse) looks accepted without resizing anything. Catch it
up-front with a clear path forward. `lookup` returns empty on `helm template`/client dry-run, so
render/CI paths are unaffected; the comparison is textual (write the same unit as the live object,
normally the one this chart installed).
