# chart/templates/openfga-migrate.yaml

Rationale for `chart/templates/openfga-migrate.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `volumeMounts:`

kept uniform with its siblings; the binary writes nothing itself, which a read-only rootfs turns into a failure. HOME on the tmp
emptyDir is the pair every app container in this chart already uses.

## `securityContext: { fsGroup: 65532 }`

The container runs as uid 65532 (the OpenFGA image's own user) and a secret volume's files are root's, so the pod's fsGroup
is what lets `openfga migrate` read its 0440 config file. Without it the read fails, OpenFGA's `cmd/root.go` drops that
error, and the migrate runs with an empty DSN.

## `- { name: openfga-config, mountPath: /etc/openfga, readOnly: true }`

The datastore DSN carries the AGE password, so it reaches the Job as a mounted file, never as an argument or an env
value ([[XC-081]]). `openfga migrate` reads `datastore.uri` from `/etc/openfga/config.yaml` (OpenFGA v1.18.3
`cmd/root.go`: flags, then `OPENFGA_*` env, then the file); the file is the infra-credentials Secret's
`openfga-config.yaml`, the same key the server mounts. `--datastore-engine` stays an argument: it is no secret.

## `- { name: HOME, value: /tmp }`

HOME on the tmp emptyDir: this container now runs with a read-only rootfs, and the CLI writes under $HOME.
