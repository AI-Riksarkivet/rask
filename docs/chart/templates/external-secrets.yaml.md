# chart/templates/external-secrets.yaml

Rationale for `chart/templates/external-secrets.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if not (.Capabilities.APIVersions.Has "external-secrets.io/v1/ExternalSecret") }}`

THE EXTERNAL SECRETS OPERATOR IS A PREREQUISITE ([[XC-004]]; owner, 2026-10-05: platform-run, rask never
installs it). Every Kubernetes Secret a pod mounts is written here from the store, and no credential
is a chart value, so with no operator nothing would deliver one: the render refuses rather than installing
a release whose pods wait on Secrets nobody creates. `.Capabilities` is the cluster's own answer on an
install or upgrade; a cluster-less render states the target with `--api-versions`.

## `{{- range $key, $name := include "lance.devCredentials" . | fromJson }}`

AN ESTATE THAT ALREADY HOLDS DATA KEEPS ITS CREDENTIALS. The development Postgres and the object store keep the
password and root they were initialised with, so generating fresh ones beside them hands every consumer a
credential its server refuses. Each credential is guarded on its own (`$unlocks`), and a store counts only once it
was given a credential, because each fixture is installed before rask and its StatefulSet exists on a fresh install:
`postgres-password` while the development Postgres's StatefulSet (`lance.postgresFixtureHost`) and its password's
Secret, `<host>-auth`, both exist (`$postgres`); `object-store-secret-key` while the object store's StatefulSet and its
root's Secret, `<release>-object-store-root`, both exist (`$objectStore`). On an upgrade of an estate whose store exists,
that credential's Secret must already exist: carried forward from the current credential before the first upgrade, and
rotated in place afterwards (docs/OPERATORS.md 8). A credential no store holds (one added later) is
generated when absent. `lookup` answers only against a cluster, so a cluster-less render and a fresh install, where no
store exists yet, generate.

The catalog's three database roles' passwords (`database-migrator-password`, `database-catalog-password`,
`database-maintenance-password`) take no such guard: no store is initialised with them. The postgres fixture's
provisioning Job sets each role's password from the store on every run (`ALTER ROLE ... PASSWORD`, its
`files/provision.sql`), so a regenerated one is applied at the next install of the fixture, which `make k3s-up` runs
before rask's release.

## `{{- if or (not $held) (dig "metadata" "ownerReferences" list $held) }}`

THE DEV OPENBAO'S CREDENTIALS, GENERATED IN THE CLUSTER. The render carries none: ESO's Password generator fills
each credential's own Secret (`lance.devCredentials`) once (`CreatedOnce`) and never again, and the dev OpenBao's seed
mounts them as one directory and writes each key into the in-memory store. It is a Secret rather than store-only because the store
forgets: the development Postgres and the object store keep what they were initialised with across an OpenBao
replacement, so the next seed must write the same values. `keep` holds it through an uninstall for the same
reason the data volumes are held. Rendered only for the chart's own dev store (`lance.mintsDevCredentials`);
a real deployment is refused before it gets here (prod-credentials.yaml), and a platform store holds
credentials its operator provisioned. Rotation: docs/OPERATORS.md.
A Secret that exists with no owner was carried forward by the operator from the credentials the stores already
hold, so nothing here generates beside it; one ESO created (owned by this ExternalSecret) keeps it rendered,
since removing the owner would garbage-collect the Secret.

## `{{- range $role := list "catalog" "migrator" }}`

THE CATALOG'S DATABASE CREDENTIALS, one Secret per role that holds one: `<release>-database-catalog`, mounted into the
catalog's pods, and `<release>-database-migrator`, mounted into the migration Job (ADR 0230 §8). Each carries the files
`username` (the role's name, `postgres.catalog.roles.<role>`, a literal of the template) and `password` (the store's
`postgres.catalog.secrets.<role>`), which the process reads at each new connection, never through env. Rendered in every
mode, since the catalog always keeps its database: in development the dev store holds the passwords the seed wrote from
the generated dev credentials, and in production the platform's store holds them at the same names. The maintenance
role's password has no Secret of the chart's yet, since maintenance opens no connection; the postgres fixture's
provisioning Job reads it to set the role's password. A role name the template writes is held to lowercase letters,
digits and `_`, so the name reaches the Secret, the migration Job's environment and each revision's grants exactly as
written.

## `name: {{ include "lance.fullname" . }}-infra-credentials`

THE INFRA TIER'S SECRET, for the pods that do not log in to the store and need the Postgres password: OpenFGA and its
migrate Job, each of which mounts it as a file and takes nothing from it through env ([[XC-081]]). Rendered only while
the chart runs OpenFGA (`openfga.enabled`). It carries `openfga-config.yaml`, OpenFGA's config file with the datastore
DSN assembled from the store's `postgres` password and `lance.postgresHost`, which `openfga run` and `openfga migrate` read from `/etc/openfga`
(OpenFGA v1.18.3 `cmd/root.go`). It holds no store credential: the object store's root has a Secret of its own
(below), so a pod that mounts this one for the database reads nothing that signs an STS token.

## `{{- if .Values.objectStore.enabled }}`

THE DEVELOPMENT STORE'S ROOT PAIR, `<release>-object-store-root`, rendered only with the dev store. On RustFS the root
secret signs every STS session token (GHSA-m77q-r63m-pj89, ADR 0168), so the Secret has exactly two readers, the
store (the rustfs fixture's `rootCredentials`) and the provisioning hook (`object-store-provisioning.yaml`), and ESO
fetches it from
`secret/object-store-root`, a path only ESO's store policy grants. BOTH HALVES come from
ONE object: the store mounts both as files from a non-optional secret volume, so a pair split across two sources is a
pod that cannot start rather than one that signs wrongly. No lakehouse workload reads a store key from this file or any
other: each one's store identity is its ServiceAccount token ([[XC-084]]). A platform's S3 has no such Secret here: its
operator holds its root and provisions rask's identities. `tests/chart/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`
gates who reads it.

## `{{- if hasKey (include "lance.natsFiles" . | fromJson) "object-store-bus-creds" }}`

The development object store's NATS credential, a creds file: the static user `object-store` (values.yaml `nats.auth`),
which publishes each object write on `store.events`. A static user has its JWT and seed in the dev store as
`nats-user-object-store`, which the object store reads as a file, never an env value. The stream Job and every lakehouse pod are
session users and have no such file. The NATS fixture writes nats-box's `monitor` creds, the server's operator-mode
config and the stand-in signer's keys with its own ExternalSecrets.

## `apiVersion: external-secrets.io/v1`

A third party's credential, provisioned in the store by the operator (values.yaml `secrets`).
