# chart/templates/backup-pg.yaml

Rationale for `chart/templates/backup-pg.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `serviceAccountName: {{ include "lance.saName" (list . "backup-pg") }}`

THE DUMP'S OWN IDENTITY ([[XC-084]], ADR 0170). Its account is `<release>-sa-backup-pg` (`lance.saComponents` renders it
while `backups.pgDump` is on), and the store grants that account's token the policy written under its name
(`lance.objectStorePolicy`, `<release>-object-store-identities`): list, write and prune under `_backups/pg/` in
`objectStore.bucket`, and nothing else. It shares no account with the control-root backup, so neither can write the
other's backups.

## `- { name: pgpass, emptyDir: { medium: Memory, sizeLimit: 1Mi } }`

PG_DUMP READS ITS PASSWORD FROM A FILE, never the environment ([[XC-084]]). libpq takes a password from `PGPASSFILE`
only when the file grants no group or world access, and a secret volume under the pod's `fsGroup` is group-readable, so
pg_dump cannot read the mounted `postgres-password` file directly. The init container writes the one line pg_dump needs
(`host:port:*:user:password`, the password's `\` and `:` escaped as the format requires) into this memory volume under
`umask 077`, so the file is 0600, lives in no node disk and ends with the pod.

## `{ pg_dump -h {{ include "lance.ageConnectHost" . }} -p {{ .Values.age.port }} -U {{ .Values.age.user }} -d "$db" || touch /tmp/.dump-failed ; } | gzip > /dump/"$db".sql.gz`

NOT `pg_dump | gzip`. A pipeline reports only the LAST command's status, and this is `sh`, dash, where
`set -o pipefail` does not exist. So a pg_dump that dies mid-stream still leaves a structurally valid gzip and the
pipeline exits 0: `set -e` sees a clean run and the upload container ships a TRUNCATED dump as that day's backup. That
is the failure a backup can least afford, because nothing reveals it until a restore. Streaming is kept on purpose (an
uncompressed dump would have to fit the emptyDir); the marker file carries pg_dump's real status out of the subshell,
and lives in /tmp rather than /dump so it can never be uploaded into the backup prefix.

## `[ -s /dump/"$db".sql.gz ] || { echo "no dump produced for database $db" >&2; exit 1; }`

Catches the dump file never being written at all. It does NOT catch truncation (a truncated gzip is non-empty), which
is what the marker above is for.

## `{{- with .Values.imagePullSecrets }}`

A FIRST-PARTY IMAGE NEEDS THE ESTATE'S PULL SECRETS: running the rask image makes this
an ordinary first-party workload, which takes the same pull secrets as every other.

## `image: "{{ include "lance.catalogImage" . }}"`

THE ESTATE'S OWN S3 SEAM, not a third-party CLI image a registry can refuse to
serve anonymously ([[XC-075]]). What this uploads is the authorization store
(OpenFGA), so a CronJob that cannot pull its image backs the estate's most
irreplaceable database up NOWHERE and says so once in an event nobody reads.
Every verb is plain S3: `put_object`, and the retention walk that
`storage.prune_timestamped_prefixes` owns for both backup lanes.

## `{{- include "lance.objectStoreSessionEnv" (list . "RASK" "backup-pg") | nindent 16 }}`

THE RUN HOLDS NO KEY. The upload exchanges the token its pod projects for `objectStore.identity.audience` for one session
(`lakehouse.store_session.job_client`, `RASK_S3_*`) and signs the whole run with it; the role ARN is
`objectStore.identity.roleArns.backup-pg`, which RustFS ignores and AWS assumes. A run missing any of these fails at its
start rather than signing with whatever boto3's default chain finds, which on a cloud node is the node's own role.

## `- { name: RASK_S3_REGION, value: "us-east-1" }`

THE LITERAL, as `services.yaml` and `maintenance.yaml` already spell it. A
`objectStore.region` value would be a fourth spelling of one constant nothing in this estate varies.

## `- --keep`

`--keep 0` means UNBOUNDED in the script, which is the same meaning this
template's `gt 0` gate always had — so an operator who sets keep to 0 gets
growth rather than a surprise deletion.
