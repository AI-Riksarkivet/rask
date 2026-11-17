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

## `name: {{ include "lance.fullname" . }}-openfga-migrate-{{ $spec | sha256sum | trunc 8 }}`

The Job is named by a digest of its own rendered spec (`lance.openfgaMigrateSpec`: the image, the datastore it reaches,
its arguments), so an upgrade that changes none of them leaves the same object in place and runs nothing, and one that
changes any of them creates the next Job (XC-160), under Helm and under Argo CD alike: it is an ordinary object to both,
with no hook annotation (a hook runs on every sync whatever its name) and no TTL (a reaped Job would be created and run
again by the next upgrade). The finished Job stays until its inputs change, when the release removes it for the next.
Pinned by `tests/chart/test_an_upgrade_that_changes_no_input_of_the_migration_runs_none.py`.
