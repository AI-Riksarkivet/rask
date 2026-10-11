# chart/templates/minio-buckets.yaml

Rationale for `chart/templates/minio-buckets.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

Bucket-init: the ONLY provisioner of the platform buckets, in-cluster or minio.externalEndpoint,
so each is CREATED and none is waited for (see `minio.buckets` in values.yaml). NOT a hook — see
"BOOTSTRAP JOBS" in _helpers.tpl: the lakehouse apps need these buckets at startup, and a
post-install hook fails only after `--wait` blamed something else.

## `{{- with .Values.imagePullSecrets }}`

A FIRST-PARTY IMAGE NEEDS THE ESTATE'S PULL SECRETS, which `minio/mc` never did: it came
from a public registry, so this Job was the one pod spec that could skip them. Running the
rask image makes it an ordinary first-party workload, which takes the same pull secrets
as every other.

## `image: "{{ include "lance.catalogImage" . }}"`

THE ESTATE'S OWN S3 SEAM, not a third-party CLI. `minio/mc` refuses ANONYMOUS pulls
on every registry this estate can reach — measured 2026-09-24 with the bearer
decoded, Docker Hub answers `access: []` and quay.io `actions: []`, both 401 on the
manifest — so on a node that pulls fresh this Job never starts and every bucket the
lakehouse needs is simply absent ([[XC-075]]). `scripts/ensure_bucket.py` runs out
of the rask image, exactly as the backup CronJob runs `control_root_backup.py`, and
drives `packages/storage`'s `s3_client`: the same S3 client the fleet uses, in an
image no registry can withdraw. Both halves below are plain S3 calls — `mb` is
`create_bucket`, `ls` is `head_bucket`.

`lance.catalogImage`, not a second name for the same artefact: the catalog family
already runs one image under many entrypoints, and naming it again here would be
a second specification of one image, free to drift from the first. It also means
a side-loaded stack builds nothing extra for this Job.

## `- { name: AWS_REGION, value: "us-east-1" }`

THE LITERAL, as `services.yaml`, both maintenance templates and
`_ray-cluster-config.tpl` already spell it. A `minio.region` value would be a fifth
spelling of one constant nothing in this estate varies.
