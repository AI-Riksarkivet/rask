# chart/templates/object-store-provisioning.yaml

Rationale for `chart/templates/object-store-provisioning.yaml`. It lives here because Helm stores the chart's templates
in every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

THE PLATFORM BUCKETS AND EVERY IDENTITY'S POLICY: the one provisioning step for the development store, rendered whenever
`objectStore.enabled`. It creates every bucket the estate makes for itself (below), and writes each lakehouse
workload's canned policy under its ServiceAccount's account name, the documents `<release>-object-store-identities`
declares (`object-store-identities.yaml`): the catalog, lineage, maintenance and each backup CronJob that is on. No user holds a
policy: the store grants one to the token the account's pod projects (ADR 0170, ADR 0197), so the run makes no user and
reads no secret but the root's. It is the only workload that presents the root (ADR 0168;
`tests/unit/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`). A platform's S3 gets neither from rask: its
operator makes the buckets and creates the policies from the same ConfigMap.

IDEMPOTENT, CALL BY CALL, on RustFS's native admin API (`scripts/rustfs_admin.py`): `add-canned-policy` replaces a policy
of the same name, so a re-run converges on what git declares. The script exits non-zero on the first refusal, so a
malformed policy fails the hook instead of leaving an identity on its previous policy while the hook reports success.
Its pruning runs (`--prune`) then remove every regular user but the root, and every rask policy (`rask-*`, or named after
a ServiceAccount) the plan does not create, so the store's identities are what git declares and a retired one is not
left signing ([[XC-083]], [[XC-084]]: the static `rask-maintenance`, `rask-lineage` and `rask-backup` users). A policy
rask does not own, RustFS's built-ins or an operator's, is never removed. A canned policy must carry `Version`
(`rustfs@1.0.3:rustfs/src/admin/handlers/policies.rs:229-231`); every policy here does.

## `"helm.sh/hook": {{ $phase }}`

ONE JOB PER PHASE, AND EACH ANSWERS A DIFFERENT FAILURE. The provisioning Job prunes nothing, and a second one
(`…-prune-<rev>`) provisions again and prunes: a policy this release retires is still what the previous release's pods
sign under until the upgrade has replaced them, so removing it before then cuts them off mid-flight. On a fresh install
the provisioning Job is a plain release object (a `pre-install` hook would wait for a root credential the release itself
creates) and the prune is `post-install`; on an upgrade they are `pre-upgrade` and `post-upgrade`. Under Argo CD, which
renders every sync as an install, the provisioning Job is a `Sync` hook and the prune a `PostSync` one
(`lance.reconciledJob`), so the prune still waits for the sync's resources to be Healthy ([[XC-164]]). A plain
`helm install` without `--wait-for-jobs` can start the prune while the provisioning Job still runs; both write the same
declared set, so the overlap removes nothing either needs (`make k3s-up` waits for Jobs).

`pre-upgrade` is what lets a new identity exist before the pods that present it roll: a pod whose account names no
policy is refused every exchange, and its boot fails closed until the policy exists.

A POLICY NEVER PRECEDES ITS BUCKET: `scripts/rustfs_admin.py` creates every `--bucket` before it writes any policy, so a
bucket an upgrade adds exists before the policy that names it, which the store would otherwise accept and then silently
govern nothing with.

A PRE-UPGRADE HOOK MAY ONLY CONSUME A SECRET KEY THE PREVIOUS RELEASE ALREADY PUBLISHED — a permanent property of the
phase, and a CONSTRAINT ON RENAMES rather than a reason to drop the phase. This Job mounts the store's root credential
from `<release>-object-store-root`, which ESO writes from the dev store's `secret/object-store-root`; the mapping that
names a key arrives with the release's OWN templates, which helm applies only AFTER its pre-upgrade hooks. So an upgrade
that RENAMES that key deadlocks: the hook cannot start (`CreateContainerConfigError: couldn't find key`), helm waits for
it, and the ExternalSecret that would create the key sits in the manifest helm has not applied. Measured on the live
estate 2026-09-11.

## `"helm.sh/hook-weight": "5"`

Ordering WITHIN a phase: after `openfga-model` (0) and before `bootstrap-admin` (10).

## `backoffLimit: 20`

THE STORE MAY START AFTER THE RELEASE. The rustfs fixture takes its root from `<release>-object-store-root`, which ESO
writes only once rask is installed, so on an empty cluster the store comes up after this Job's first pods
(deploy/fixtures/rustfs/fixture.env). Each pod's wait for the store is bounded (`rustfs_admin.py`), and twenty
retries outlast that start rather than failing the install on it; `lance.bootstrapJobDeadline` still bounds the
whole Job.

## `limits: { cpu: "200m", memory: "256Mi" }`

One Python process making a handful of HTTP calls through boto3/botocore. Not measured on the kind node yet.

## `ttlSecondsAfterFinished: 3600`

Reap the finished pod, exactly as `openfga-migrate.yaml` does and for the identical reason: this Job's name carries the
bootstrap revision, so the `before-hook-creation` policy above matches the PREVIOUS revision's name never, and each
upgrade mints a new name and leaves the old husk holding its completed pod. Counted live 2026-09-16: 17 of them
(r146..r163).

## `securityContext: { fsGroup: 10001 }`

The container runs as 10001, rask's image user, and reads the root key from a 0400 secret volume, which kubelet writes
as root:root unless the pod names an fsGroup; with one it is root:<fsGroup> and group-readable.

## `volumes:`

THE ROOT PAIR IS A TRUST ROOT. RustFS signs every STS session token with the root secret, so whoever reads it can forge
any session (GHSA-m77q-r63m-pj89; `.claude/skills/rask-rustfs/SKILL.md` § 3). This hook and the store are the only
readers of `<release>-object-store-root`, the Secret ESO writes it into, and here it is a FILE, never env or argv.

## `image: {{ include "lance.catalogImage" $ | quote }}`

rask's own image: python with boto3/botocore for the admin calls, so the hook pulls nothing the estate does not build
and depends on no third-party CLI image a registry could withdraw ([[XC-075]]). It passes `args` and keeps the image's
tini as PID 1 (`rask-helm` § 6).

## `- name: provision`

RustFS's NATIVE admin API under `/rustfs/admin/v3`, SigV4-signed (service `s3`) with the root pair and plain JSON
bodies, the calls the 2026-10-18 measurement drove (`scripts/rustfs_sts_measure.py`; the audit is in git history at
64ef118a9). The admin API's compatibility alias wants encrypted bodies and is not used. `scripts/rustfs_admin.py` reads
the root secret from its file and prints no secret; a store that never answers fails it after a bounded wait.

## `{{- range compact $buckets | uniq }}`

THE PLATFORM BUCKETS, created here and nowhere else on the dev store: `objectStore.bucket` (the control root),
`objectStore.buckets` (`lance.platformBuckets`), every `medallion.buckets` zone, and each multi-base data base's bucket.
The store is a plain StatefulSet that provisions none, so a bucket this list omits never appears. Warehouse buckets are
the catalog's, made at runtime by `POST /management/v1/warehouses`. `CreateBucket` on an existing bucket is accepted as
already there, so a re-run makes only what is missing, and nothing here deletes a bucket.

## `{{- range $component := include "lance.objectStoreIdentities" $ | fromJsonArray }}`

ONE POLICY PER ACCOUNT, named `system:serviceaccount:<ns>:<release>-sa-<component>` (`lance.saAccount`), the `sub` of the
token that account's pod projects. The list is `lance.objectStoreIdentities` and each document `lance.objectStorePolicy`,
the same two the identities ConfigMap renders, so the hook writes exactly the policies the render declares.

THE DOCUMENT TRAVELS IN THE HOOK'S OWN ARGUMENTS, never a mounted ConfigMap: a `pre-upgrade` hook runs before helm
applies the release's manifest, so a mounted ConfigMap would be the PREVIOUS revision's, and on the upgrade that first
renders it absent, which keeps the pod from starting and deadlocks the upgrade. Inline, the provisioning run writes the
policies of the revision it belongs to before that revision's pods roll.

## `{{- range $.Values.catalog.multibase.dataBases | default list }}`

Each multibase data base's bucket, created on the development store beside the estate's own. A data base is its tables'
storage on the estate's store, reached under the catalog's own identity and the maintain-tier vend ([[LH-286]]), so the
bucket is the store's to hold and no identity of its own goes with it.
