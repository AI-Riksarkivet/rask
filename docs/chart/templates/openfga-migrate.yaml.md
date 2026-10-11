# chart/templates/openfga-migrate.yaml

Rationale for `chart/templates/openfga-migrate.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `volumeMounts:`

kept uniform with its siblings; the binary writes nothing itself, which a read-only rootfs turns into a failure. HOME on the tmp
emptyDir is the pair every app container in this chart already uses.

## `- { name: HOME, value: /tmp }`

HOME on the tmp emptyDir: this container now runs with a read-only rootfs, and the CLI writes under $HOME.
