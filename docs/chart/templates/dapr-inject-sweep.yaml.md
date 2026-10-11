# chart/templates/dapr-inject-sweep.yaml

Rationale for `chart/templates/dapr-inject-sweep.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `env:`

kubectl WRITES, which is what a read-only rootfs turns into a failure: it caches
discovery under $HOME, and the image's HOME is a path this container may no longer
write. Pointing HOME at the tmp emptyDir is what makes the hardening survivable, and
it is the same pair every app container in this chart already uses.
