# chart/templates/external-secrets.yaml

Rationale for `chart/templates/external-secrets.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if not (.Capabilities.APIVersions.Has "external-secrets.io/v1/ExternalSecret") }}`

THE EXTERNAL SECRETS OPERATOR IS A PREREQUISITE ([[XC-004]]; owner, 2026-10-05: platform-run, rask never
installs it). Every Secret a pod without a sidecar mounts is written here from the store, and no credential
is a chart value, so with no operator nothing would deliver one: the render refuses rather than installing
a release whose pods wait on Secrets nobody creates. `.Capabilities` is the cluster's own answer on an
install or upgrade; a cluster-less render states the target with `--api-versions`.

## `{{- $stores := or (lookup "apps/v1" "StatefulSet" .Release.Namespace (include "lance.ageHost" .)) $objectStore }}`

AN ESTATE THAT ALREADY HOLDS DATA KEEPS ITS CREDENTIALS. The AGE database and the object store keep the
password and root they were initialised with, so generating fresh ones beside them hands every consumer a
credential its server refuses. The object store counts only when its StatefulSet and its root's Secret,
`<release>-object-store-root`, both exist (`$objectStore`). On an upgrade of an estate whose stores exist, the credential Secret must
already exist: created from the current credentials before the first upgrade, and rotated in place
afterwards (docs/OPERATORS.md 8). `lookup` answers only against a cluster, so a cluster-less render and a
fresh install, where no store exists yet, generate.

## `{{- if or (not $held) (dig "metadata" "ownerReferences" list $held) }}`

THE DEV OPENBAO'S CREDENTIALS, GENERATED IN THE CLUSTER. The render carries none: ESO's Password generator
fills `<release>-dev-credentials` once (`CreatedOnce`) and never again, and the dev OpenBao's seed mounts it
and writes each key into the in-memory store. It is a Secret rather than store-only because the store
forgets: the AGE database and the object store keep what they were initialised with across an OpenBao
replacement, so the next seed must write the same values. `keep` holds it through an uninstall for the same
reason the data volumes are held. Rendered only for the chart's own dev store (`lance.mintsDevCredentials`);
a real deployment is refused before it gets here (prod-credentials.yaml), and a platform store holds
credentials its operator provisioned. Rotation: docs/OPERATORS.md.
A Secret that exists with no owner was carried forward by the operator from the credentials the stores already
hold, so nothing here generates beside it; one ESO created (owned by this ExternalSecret) keeps it rendered,
since removing the owner would garbage-collect the Secret.

## `name: {{ include "lance.fullname" . }}-infra-credentials`

THE INFRA TIER'S SECRET, for the pods without a sidecar that need the AGE password: the AGE server and openfga
migrate. It carries `postgres-password`, and `openfga-datastore-uri` assembled from it, and no store credential: the
object store's root has a Secret of its own (below), so a pod that mounts this one for the database reads nothing
that signs an STS token.

## `{{- if .Values.objectStore.enabled }}`

THE DEVELOPMENT STORE'S ROOT PAIR, `<release>-object-store-root`, rendered only with the dev store. On RustFS the root
secret signs every STS session token (GHSA-m77q-r63m-pj89, ADR 0168), so the Secret has exactly two readers, the
store (the rustfs fixture's `rootCredentials`) and the scoped-users hook, and ESO fetches it from
`secret/object-store-root`, a path every Dapr app's Configuration denies (`lance.daprAppSpec`). BOTH HALVES come from
ONE object: the store mounts both as files from a non-optional secret volume, so a pair split across two sources is a
pod that cannot start rather than one that signs wrongly. A platform's S3 has no such Secret here: its operator holds
its root and provisions rask's identities. `tests/unit/test_the_root_storage_secret_reaches_only_its_users.py` gates
who reads it.

## `apiVersion: external-secrets.io/v1`

The in-chart Collector's credential for the audit store, mounted as files (otel-collector.yaml).

## `{{- if .Values.ray.auth.enabled }}`

Gated on ray.auth.enabled ALONE, matching `rask.rayAuthEnv`, which emits the consuming
secretKeyRef on that value by itself. Not on `ray.enabled`/`singleTenant.enabled`: configmap.yaml
states that "rask's Ray is managed outside this repo, so an external address is the NORMAL case",
and the fleet must present a bearer token to an external cluster too (measured 2026-08-16: a
narrower gate left rask-compute in CreateContainerConfigError and the release in pending-upgrade).

## `{{- if include "lance.backupIdentity" . }}`

THE BACKUPS' IDENTITY, `<release>-backup-s3`, one Secret for both backup CronJobs and rendered when either is on. It
holds the scoped pair `objectStore.backupAccessKey` names (the `rask-backup` policy in object-store-scoped-users.yaml),
fetched from `secret/backup-s3`, and never the store's root.

A CronJob pod carries no Dapr sidecar, so the secret-store path is unavailable to it and ESO is the
one the estate admits. Both consumers MOUNT it as a file rather than taking a `secretKeyRef` env row, and each
reads the shape its tool takes: `secret-key` for the pg dump's uploader (`RASK_S3_SECRET_KEY_FILE`, the access key
id is config), and `credentials.json`, the pair as one JSON file, for `control_root_backup.py`, whose
`--credentials-file` is the only way it accepts a credential without falling back to the environment chain. One
file holding both halves means a partial write never looks like a valid credential.

## `{{- if hasKey (include "lance.natsFiles" . | fromJson) "nats-admin-creds" }}`

The stream Job's NATS credential, the admin user's creds file (values.yaml `nats.auth`). The bus is a prerequisite (ADR 0153): the
NATS fixture writes nats-box's `monitor` creds and the server's operator-mode and route includes with its own ExternalSecrets.

## `apiVersion: external-secrets.io/v1`

THE DAPR APP-API TOKEN, for the injector. A pod annotated `dapr.io/app-token-secret: <this>` has daprd
stamp it as `dapr-api-token` on every delivery, and the app compares it with the store's `dapr-app-token`
(`RASK_APP_TOKEN_FROM_STORE`) or with `APP_API_TOKEN` (`lance.appTokenEnv`). Both read the one store field,
so the two sides agree by construction. Gated on either consumer: `dapr.enabled` (the annotation) or
`auth.enabled` (the env row), so neither reference can dangle. A rotation reaches a sidecar only when its
pod restarts, since the injector reads the Secret at pod creation (docs/OPERATORS.md).

The zones' session-cookie sealing key (AES-256-GCM) and the confidential client secret, shared by
every zone (they all read the origin-wide cookie; home also exchanges the code). Rotating the
sealing key signs everyone out, and reaches a zone only when it restarts.

A third party's credential, provisioned in the store by the operator (values.yaml `secrets`).
