# chart/templates/backup-pg.yaml

Rationale for `chart/templates/backup-pg.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- with .Values.imagePullSecrets }}`

A FIRST-PARTY IMAGE NEEDS THE ESTATE'S PULL SECRETS, which `minio/mc` never did:
it came from a public registry, so this pod spec was one of the few that could skip
them. Running the rask image makes it an ordinary first-party workload.

## `image: "{{ include "lance.catalogImage" . }}"`

THE ESTATE'S OWN S3 SEAM, not a third-party CLI. `minio/mc` refuses ANONYMOUS
pulls on every registry this estate can reach — measured 2026-09-24 with the
bearer decoded, Docker Hub answers `access: []` and quay.io `actions: []`, both
401 ([[XC-075]]). What this uploads is the lineage graph (AGE) and the
authorization store (OpenFGA), so on a node that pulls fresh the estate's two
most irreplaceable databases back up NOWHERE, and a CronJob that cannot start
says so once in an event nobody reads. Every verb was plain S3: `cp` is
`put_object`, `ls` + `rm` are the retention walk that
`storage.prune_timestamped_prefixes` now owns for both backup lanes.

## `- { name: AWS_REGION, value: "us-east-1" }`

THE LITERAL, as `services.yaml`, both maintenance templates and
`_ray-cluster-config.tpl` already spell it. A `minio.region` value would be a
fifth spelling of one constant nothing in this estate varies.

## `- --keep`

`--keep 0` means UNBOUNDED in the script, which is the same meaning this
template's `gt 0` gate always had — so an operator who sets keep to 0 gets
growth rather than a surprise deletion.
