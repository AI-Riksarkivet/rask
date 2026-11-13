# chart/templates/age-postgres.yaml

Rationale for `chart/templates/age-postgres.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $existingSts := lookup "apps/v1" "StatefulSet" .Release.Namespace (include "lance.ageHost" .) }}`

Guard: StatefulSet volumeClaimTemplates are immutable, so bumping age.storage on an upgrade
either hard-fails deep in the apply or (worse) looks accepted without resizing anything. Catch it
up-front with a clear path forward. `lookup` returns empty on `helm template`/client dry-run, so
render/CI paths are unaffected; the comparison is textual (write the same unit as the live object,
normally the one this chart installed).

## `- { name: POSTGRES_PASSWORD_FILE, value: /run/secrets/postgres/password }`

The password reaches the server as a mounted file, never as env ([[XC-081]]): the image's entrypoint reads
`POSTGRES_PASSWORD_FILE` in `docker_setup_env` while still root, exports the value for `initdb` and unsets the file
variable before it re-runs itself as `postgres` (read off the running image's `/usr/local/bin/docker-entrypoint.sh`,
2026-11-13). So mode `0440` serves both a root start and a pod whose `security.infraContexts` sets `fsGroup` 999. The
file is the infra-credentials Secret's `postgres-password`, the key the backup CronJob mounts too.
