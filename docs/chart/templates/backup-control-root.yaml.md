# chart/templates/backup-control-root.yaml

Rationale for `chart/templates/backup-control-root.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

The CONTROL ROOT's scheduled backup ([[LH-110]]) — the registry holding every project, warehouse,
namespace binding and bucket claim. Lose it and the Lance data survives while nothing can route to
it, which is the one loss the lakehouse cannot reconstruct from storage.

`scripts/control_root_backup.py` already shipped backup/verify/restore/prune and
`docs/runbooks/RUNBOOK-restore.md` documents the drill; what did not exist was anything that RUNS it.
The tool is baked into the catalog image (one file, not all of `scripts/`) so this job and the
service share one implementation — a shell reimplementation with an S3 CLI would be a second one to keep
in step, and a ConfigMap copy would be a third place the same code lives.

THE RUN HOLDS NO KEY ([[XC-084]], ADR 0170). The job runs as its own ServiceAccount,
`<release>-sa-backup-control-root` (`lance.saComponents` renders it while `backups.controlRoot` is on), projects a token
for `objectStore.identity.audience`, and exchanges it once per run at STS `AssumeRoleWithWebIdentity` for the session
it signs the whole run with (`lakehouse.store_session.JobStoreIdentity`, `job_client`; env `RASK_S3_*` from
`lance.objectStoreSessionEnv`, role `objectStore.identity.roleArns.backup-control-root`). The token file, the STS
endpoint and the role are required, so a run missing one fails at its start rather than signing with whatever boto3's
default chain finds, which on a cloud node is the node's own role. The session's reach is the canned policy written
under the account's name (`lance.objectStorePolicy`, `<release>-object-store-identities`): read the control-root record
prefixes it copies (`_projects`, `_warehouses`, `_trash`, `_protection`, `_policies`, `_gates`, `_bases`), and write and
prune under `backups.controlRoot.dest` (default `<control bucket>/_backups/control`), and nothing else. It shares no
account with the pg dump, so neither can write the other's backups.

The component label is load-bearing under the prod default-deny NetworkPolicy, exactly as
`backup-pg.yaml` records: an unlabelled pod matches no allow rule and its writes are dropped in
silence.

## `image: "{{ include "lance.catalogImage" . }}"`

The SAME helper the sibling hooks use (`bootstrap-admin`, `openfga-model`). Spelling
the stem by hand here produced `rest-catalog` against a chart default of
`lance-rest-catalog`, which is a reference that exists in no registry — the Job sat
in ImagePullBackOff and the CronJob looked healthy.
