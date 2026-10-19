# chart/templates/object-store-scoped-users.yaml

Rationale for `chart/templates/object-store-scoped-users.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

SCOPED USERS AND PLATFORM BUCKETS — the one provisioning step for the development store, rendered whenever
`objectStore.enabled`. It creates every bucket the estate makes for itself (below), every plane identity named in
values.yaml `objectStore` (maintenance, lineage, viewer, the media corpus readers, the backups, and each multi-base data
base), and the catalog's policy, named after its ServiceAccount and held by no user: the store grants it to the token
the catalog's pod projects (ADR 0170). Without a step in the release, a scoped identity is a values knob only a hand-run
admin session can turn, and a fresh install comes up on the store's root. It is the only workload that presents the root
(ADR 0168; `tests/unit/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`). A platform's S3 gets neither from
rask: its operator makes the buckets and the identities.

WHY A POLICY AND NOT JUST A USER: the point of this Job is the DENY — the maintenance credential must not
write `_projects/`, `_protection/`, `_warehouses/`, `_gates/` or `__manifest/`, the records that decide what
maintenance is permitted to do. A credential that can rewrite its own governing records can turn off the
shallow-clone guard that stops a compaction destroying another dataset's source. And on RustFS a binding that names a
missing policy refuses every request the user signs, so every policy is written before any binding names it.

THREE POLICY LESSONS, each of which cost a broken pipeline, and none of which is recoverable by
reading the docs:

* `s3:ListBucket` is BUCKET-scoped, not object-scoped. An object-scoped rule constrains a LIST not
at all — the first attempt allowed the whole control plane to be listed and looked like the
store ignoring Deny.
* a prefix condition must match both `<p>` and `<p>/*` (the base policies below list both): Lance
lists with `prefix=<p>/`, and `StringLike` does not match a trailing-slash pattern against it.
* a bucket-NAME allow-list cannot govern this estate at all, because the buckets that hold tenant
data are minted at runtime by `POST /management/v1/warehouses`. See the long note above the policies.

IDEMPOTENT, CALL BY CALL, on RustFS's native admin API (`scripts/rustfs_admin.py`):
`add-canned-policy` replaces a policy of the same name; `add-user` on an existing access key sets its secret
again, which is what makes a derived secret converge; `set-user-or-group-policy` replaces the user's binding.
The script creates every policy before it binds any, and exits non-zero on the first refusal, so a malformed policy or
a denied call fails the hook instead of leaving a credential on its previous policy while the hook reports success. Its
pruning runs (`--prune`) then remove every regular user and every rask policy (`rask-*`, or named after a ServiceAccount)
the plan does not declare, so the store's identities are what git declares and a retired one is not left signing
([[XC-083]]: the catalog's `rask-catalog` user). A policy rask does not own, RustFS's built-ins or an operator's, is
never removed. A canned policy must carry `Version` (`rustfs@1.0.3:rustfs/src/admin/handlers/policies.rs:229-231`);
every policy here does.

## `"helm.sh/hook": {{ $phase }}`

ONE JOB PER PHASE, AND EACH ANSWERS A DIFFERENT FAILURE; on a fresh install the Job is a plain release object that
provisions and prunes, since no pod of an earlier release can be presenting a retired identity. On an upgrade the
`pre-upgrade` Job provisions and prunes nothing, and the `post-upgrade` one (`…-prune-<rev>`) provisions again and
prunes: a user this release retires is still what the previous release's pods present until the upgrade has replaced
them, so removing it before then cuts them off mid-flight.
`pre-upgrade` is what lets the identities be NAMED by default. With only `post-upgrade` the
store's user is created after the Deployments have been updated, so pods restart holding a
credential the object store has never heard of and 403 until the hook catches up — and that
window is the entire reason every scoped identity shipped defaulted to the store's ROOT key.

A POLICY NEVER PRECEDES ITS BUCKET: `scripts/rustfs_admin.py` creates every `--bucket` before it writes any policy,
so a bucket an upgrade adds exists before the policy that names it, which the store would otherwise accept and then
silently govern nothing with. The `post-upgrade` pass runs the same idempotent plan again once the release's own
objects are applied — sound because `add-canned-policy` and `add-user` replace.

A PRE-UPGRADE HOOK MAY ONLY CONSUME A SECRET KEY THE PREVIOUS RELEASE ALREADY PUBLISHED —
a permanent property of the phase, and a CONSTRAINT ON RENAMES rather than a reason to drop
the phase. This Job mounts the store's root credential from `<release>-object-store-root`, which ESO
writes from the dev store's `secret/object-store-root`; the mapping that names a key arrives with the release's OWN templates, which helm
applies only AFTER its pre-upgrade hooks. So an upgrade that RENAMES that key deadlocks: the
hook cannot start (`CreateContainerConfigError: couldn't find key`), helm waits for it, and
the ExternalSecret that would create the key sits in the manifest helm has not applied.
Measured on the live estate 2026-09-11 — seeding the property into OpenBao does not break it
either, and a hand-patched Secret is reverted by ESO within seconds, because ESO reconciles
continuously rather than on its refresh interval.

DROPPING THE PHASE IS THE WRONG REPAIR, and was tried: it reopens exactly the window this Job
exists to close, and `tests/unit/test_a_provisioned_identity_is_one_the_service_uses.py`
fails on it by design.

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

Reap the finished pod, exactly as `openfga-migrate.yaml` does and for the identical reason: this
Job's name carries the bootstrap revision, so the `before-hook-creation` policy above matches the
PREVIOUS revision's name never — each upgrade mints a new name and leaves the old husk holding its
completed pod. Counted live 2026-09-16: 17 of them (r146..r163). The cost is legibility, which is
the thing this estate keeps losing — a `kubectl get pods` full of husks is how four orphaned
durables and a split image stem both stayed invisible.

## `securityContext: { fsGroup: 10001 }`

Both containers run as 10001, rask's image user, and read the root key from a 0400 secret volume, which kubelet
writes as root:root unless the pod names an fsGroup; with one it is root:<fsGroup> and group-readable.

## `volumes:`

THE ROOT PAIR IS A TRUST ROOT. RustFS signs every STS session token with the root secret, so whoever reads it can
forge any vended credential (GHSA-m77q-r63m-pj89; `.claude/skills/rask-rustfs/SKILL.md` § 3). This hook and the
store are the only readers of `<release>-object-store-root`, the Secret ESO writes it into. The scoped secrets live in a memory-backed emptyDir that exists for the pod's lifetime only.

## `initContainers:`

EVERY SECRET IS A FILE ([[XC-004]]): the root as a mounted key of the ESO-written `<release>-object-store-root`, and each
scoped secret derived from it in memory by `lance.scopedSecretFn` — the function the dev OpenBao's seed runs, so
the user this hook creates and the field a service reads agree without one handing the other a key. No key is in
the pod spec, an env, argv or the render.

## `image: {{ include "lance.catalogImage" . | quote }}`

rask's own image for both containers: it carries `sh`, coreutils' `sha256sum` and `cut` for the derivation, and
python with boto3/botocore for the admin calls, so the hook pulls nothing the estate does not build and depends on
no third-party CLI image a registry could withdraw ([[XC-075]]).

## `- name: provision`

RustFS's NATIVE admin API under `/rustfs/admin/v3`, SigV4-signed (service `s3`) with the root pair and plain JSON
bodies, the calls the 2026-10-18 measurement drove (docs/audits/2026-10-18/rustfs-zero-trust.md). The admin
API's compatibility alias wants encrypted bodies and is not used. `scripts/rustfs_admin.py` reads the root
secret and every scoped secret from files and prints no secret; a store that never answers fails it after a bounded
wait.

## `{{- range compact $buckets | uniq }}`

THE PLATFORM BUCKETS, created here and nowhere else on the dev store: `objectStore.bucket` (the control root),
`objectStore.buckets` (`lance.platformBuckets`), every `medallion.buckets` zone, and each multi-base fixture's bucket.
The store is a plain StatefulSet that provisions none, so a bucket this list omits never appears. Warehouse buckets
are the catalog's, made at runtime by `POST /management/v1/warehouses`. `CreateBucket` on an existing bucket is
accepted as already there, so a re-run makes only what is missing, and nothing here deletes a bucket.

## `{{- if $maint }}`

THE BUCKET SET IS NOT KNOWABLE AT RENDER TIME, so the maintenance policy enumerates none.

The sweep opens buckets that no value in this chart names. `catalog.warehouses.enabled`
(on by default) provisions one physically separate bucket per warehouse at
`POST /management/v1/warehouses`, with an operator-chosen name, and the sweep enumerates those buckets itself
out of the registry (`sweep.py::_buckets_to_sweep`). An allow-list built from `objectStore.bucket` +
`medallion.buckets` + `catalog.multibase.dataBases` covers none of them: those three name the
shared bucket, the tier storage zones and the fragment-spreading allowlist, and a per-warehouse
bucket is by construction in none.

MEASURED 2026-09-04, and it is a quiet failure: with a bucket-name allow-list, `compact_one`
recorded AccessDenied per dataset as an `open:` error, which reads as a broken dataset and is
acked SUCCESS.

So the shape is: ALLOW the data plane on every bucket, and DENY the control plane precisely
(`_projects/`, `_protection/`, `_warehouses/`, `_gates/`, `__manifest/`, and any write to
`_bases/`). What is given up is bucket-name scoping, which was never enforcing anything an
operator could rely on; what is kept, in EVERY bucket rather than only the statically-named
ones, is the deny this user exists for.

WHAT THE WIDENING COSTS: a credential granted `ListBucket`/`GetBucketLocation` on
`arn:aws:s3:::*` can ENUMERATE the estate's buckets even without `s3:ListAllMyBuckets` (the
store falls back to per-bucket calls; measured 2026-09-04 with a then-deployed credential of the
same shape, 104 buckets listed), and maintenance holds `ListAllMyBuckets` outright for the
reconcile drift report. Bucket NAMES are disclosed, and object reads outside the denied control
prefixes are granted. It stays wide because a render-time allow-list cannot name a
runtime-minted warehouse. ADR 0156 C4 narrows what the maintenance identity is FOR (expiry and
purge, no rewrites; LH-467 #587, LH-473 #599); the per-table narrowing is vended credentials,
a service change rather than a policy one.

Outcomes are pinned by `tests/unit/test_scoped_policies_reach_runtime_minted_warehouses.py`,
which evaluates the rendered JSON as IAM does instead of grepping it for a bucket name — a
grep is what let the old shape render green.

## `cat >/tmp/policies/rask-lineage.json <<'POLICY'`

ITS OWN OUTBOX PREFIX, and nothing else in the store. Lineage reads no
dataset (ADR 0158): its feed and edge list are catalog-governed tables it reaches through the
catalog's doors as its own principal, never with a storage credential. What it does with this one
is the relay's drain: list `_lineage_outbox/`, read each staged event, and DELETE the object once
the event is recorded (`outbox.drop_event`).

NO PutObject ANYWHERE. `outbox.stage_event`, the write half, is called by the PRODUCERS (the
catalog, maintenance) and never here, so granting this identity a write would grant a capability
it has no code to use. The list is held to the prefix by an `s3:prefix` condition, and the object
grant names both the prefix object and what is under it, because pyarrow's S3 filesystem probes
the prefix itself before listing it.

## `cat >/tmp/policies/rask-catalog.json <<'POLICY'`

WIDE ON DATA, AND THAT IS THE POINT — what it drops is ADMIN.

NO USER HOLDS IT ([[XC-083]], ADR 0170). It is written under the catalog's account name,
`system:serviceaccount:<ns>:<release>-sa-catalog` (`lance.saAccount`), and the store's identity provider grants a
verified token exactly the canned policy its `sub` names, with no role policy (deploy/fixtures/rustfs
`identityProvider`). So the catalog's projected token is exchanged for sessions under this policy, its own IO and every
vend, the vend's narrowed further by a per-table session policy, and every other account's token maps to no policy
and is refused (measured on rustfs/rustfs 1.0.3, docs/audits/2026-10-18/rustfs_web_identity_measure.py). There is no
key to rotate: each session lives an hour at most, and the kubelet rotates the token it is exchanged for.

The DATA grant cannot be narrowed by bucket: warehouse buckets are minted at RUNTIME by
`POST /management/v1/warehouses`, so a render-time list is stale by construction — the same reason
recorded above for the maintenance policy. `s3:CreateBucket`/`s3:DeleteBucket` are here because provisioning and purge
are this service's own doors, and no other identity in the estate holds them.

WHAT ROOT HAD AND THIS DOES NOT: the admin API. No `admin:*` action of any kind, so no
user or policy calls and no service-account minting (RustFS requires an explicit
`admin:CreateServiceAccount` Allow, never deny-only) — so a compromised catalog cannot create
an identity, cannot widen its OWN policy, and cannot read the policies governing every
other plane. That is the blast-radius change; a credential that can rewrite the policy
that governs it is root wearing another name, which is exactly what
this hook's own header warns about. `s3:ForceDelete*` is also absent: `s3:*` does not imply it
on RustFS, and the purge needs only `s3:DeleteBucket`.

## `cat >/tmp/policies/rask-viewer.json <<'POLICY'`

NO WRITE OF ANY KIND, and it can have none: the object browser has no write path
at all. `viewer/api/v1/endpoints/objects.py` calls `list_objects_v2`, `head_object` and
`get_object`, and nothing else — so this identity needs no `PutObject` and no
`DeleteObject`, which not even lineage's (one scoped delete on its own outbox) can say.

THE READ STAYS WIDE ON PURPOSE. A browser that cannot list a bucket does not report a
permission error to the operator — it renders the bucket as EMPTY, which is the same
failure the store-registry fix already paid for once (`_client_for`: a bucket holding
millions of objects rendered as empty, and nothing errored). Narrowing the read here
would buy little and reintroduce the least debuggable failure this plane has.

## `cat >/tmp/policies/rask-media.json <<'POLICY'`

THE MEDIA CORPUS READER, search (`lance.mediaIdentity`): READ ONE ROOT, write nothing. It lists and opens the corpus
under `explorer.corpusS3Root` and writes nothing there, so the grant is `GetBucketLocation` and `ListBucket` on the corpus bucket, the list held
to the root's prefix by an `s3:prefix` condition on both `<p>` and `<p>/*`, and `GetObject` under that prefix. With
no prefix in the root the grant is the whole corpus bucket, and still read-only. The narrowing is possible here
because the corpus root is a render-time value, unlike the warehouse buckets the maintenance and catalog policies
must reach.

## `cat >/tmp/policies/rask-backup.json <<'POLICY'`

THE BACKUPS (`lance.backupIdentity`): READ THE RECORDS IT COPIES, WRITE ONLY ITS OWN COPIES. The control-root backup
reads the registry prefixes of `objectStore.bucket` (`_projects`, `_warehouses`, `_trash`, `_protection`,
`_policies`, `_gates`, `_bases`), and both backups write and prune under `_backups/` there and under
`backups.controlRoot.dest` when one is set. So `GetObject` covers those record prefixes and no other object;
`PutObject`, `DeleteObject` and the multipart pair cover `_backups/` and the destination only; and `ListBucket` is
held by `s3:prefix` to the same prefixes, each as `<p>` and `<p>/*`. A backup credential cannot rewrite a record it
copies, so a compromised backup pod cannot change what the catalog is permitted to do, and it holds no `admin:*`.

## `{{- range $ref, $locs := $fixture }}`

values.yaml `catalog.multibase.devSeed`: one user per credential reference, reaching only its bases.
