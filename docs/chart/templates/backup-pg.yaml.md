# chart/templates/backup-pg.yaml

Rationale for `chart/templates/backup-pg.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- with .Values.imagePullSecrets }}`

A FIRST-PARTY IMAGE NEEDS THE ESTATE'S PULL SECRETS: running the rask image makes this
an ordinary first-party workload, which takes the same pull secrets as every other.

## `image: "{{ include "lance.catalogImage" . }}"`

THE ESTATE'S OWN S3 SEAM, not a third-party CLI image a registry can refuse to
serve anonymously ([[XC-075]]). What this uploads is the authorization store
(OpenFGA) and the Dapr state store, so a CronJob that cannot pull its image backs
the estate's two most irreplaceable databases up NOWHERE and says so once in an
event nobody reads. Every verb is plain S3: `put_object`, and the retention walk
that `storage.prune_timestamped_prefixes` owns for both backup lanes.

## `- { name: AWS_ACCESS_KEY_ID, value: {{ include "lance.backupIdentity" . | quote }} }`

THE BACKUPS' SCOPED IDENTITY, never the store's root (ADR 0168). The upload writes and prunes under `_backups/pg/`
in `objectStore.bucket`, which the `rask-backup` policy grants (object-store-scoped-users.yaml), so this pod needs no
wider credential; on RustFS the root secret signs every STS session token, and a backup pod is no place to hold it.
The access key id is config; the secret half is the `secret-key` file of `<release>-backup-s3`, which ESO writes
from `secret/backup-s3` and this pod mounts read-only at 0400, read through `RASK_S3_SECRET_KEY_FILE`.
`lance.backupIdentity` fails the render when a backup is on and `objectStore.backupAccessKey` is empty.

## `- { name: AWS_REGION, value: "us-east-1" }`

THE LITERAL, as `services.yaml`, both maintenance templates and
`_ray-cluster-config.tpl` already spell it. A `objectStore.region` value would be a
fifth spelling of one constant nothing in this estate varies.

## `- --keep`

`--keep 0` means UNBOUNDED in the script, which is the same meaning this
template's `gt 0` gate always had — so an operator who sets keep to 0 gets
growth rather than a surprise deletion.
