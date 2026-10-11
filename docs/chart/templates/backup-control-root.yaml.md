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
service share one implementation — a shell reimplementation with `mc` would be a second one to keep
in step, and a ConfigMap copy would be a third place the same code lives.

THE CREDENTIAL ARRIVES AS A FILE, never on the environment. The estate admits three secret paths and
env is not among them; this pod carries no Dapr sidecar, so the one that fits is ESO writing a Secret
the pod MOUNTS. `--credentials-file` refuses to fall back to the env chain when that file is
unreadable, so a credential that moved fails the job loudly instead of silently signing as whatever
the pod's own role happens to be.

The component label is load-bearing under the prod default-deny NetworkPolicy, exactly as
`backup-pg.yaml` records: an unlabelled pod matches no allow rule and its writes are dropped in
silence.

## `image: "{{ include "lance.catalogImage" . }}"`

The SAME helper the sibling hooks use (`bootstrap-admin`, `openfga-model`). Spelling
the stem by hand here produced `rest-catalog` against a chart default of
`lance-rest-catalog`, which is a reference that exists in no registry — the Job sat
in ImagePullBackOff and the CronJob looked healthy.
