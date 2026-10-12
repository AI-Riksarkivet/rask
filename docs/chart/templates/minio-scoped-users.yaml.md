# chart/templates/minio-scoped-users.yaml

Rationale for `chart/templates/minio-scoped-users.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

SCOPED USERS — the provisioning step for every plane identity named in values.yaml `minio`
(maintenance, lineage, catalog, viewer, and each multi-base data base). Without a step in the release,
a scoped identity is a values knob only a hand-run `mc` session can turn, and a fresh install comes up
on the tenant root.

WHY A POLICY AND NOT JUST A USER: a MinIO user created with no policy gets whatever the server
defaults to, which is not a documented deny. The point of this Job is the DENY — the compaction
credential must not reach `_projects/`, `_protection/` or `_policies/`, the records that decide what
maintenance is permitted to do. A credential that can rewrite its own governing policy can turn off
the shallow-clone guard that stops a compaction destroying another dataset's source.

THREE POLICY LESSONS, each of which cost a broken pipeline, and none of which is recoverable by
reading the docs:

* `s3:ListBucket` is BUCKET-scoped, not object-scoped. An object-scoped rule constrains a LIST not
at all — the first attempt allowed the whole control plane to be listed and looked like the
store ignoring Deny.
* a prefix condition must match both `<p>` and `<p>/*` (the base policies below list both): Lance
lists with `prefix=<p>/`, and `StringLike` does not match a trailing-slash pattern against it.
* a bucket-NAME allow-list cannot govern this estate at all, because the buckets that hold tenant
data are minted at runtime by `POST /v1/warehouses`. See the long note above the policies.

IDEMPOTENT — AND RE-VERIFY IT, because the probe behind this predates the store. `mc admin policy
create` OVERWRITES an existing policy: a second create with different content returned exit 0 and the
readback showed the new document, probed 2026-09-04 against RustFS 1.0.0-beta.8, the store this
estate ran BEFORE the LH-133 swap. The `post-upgrade` pass below depends on exactly that overwrite,
so it is load-bearing rather than incidental, and it has NOT yet been re-driven against MinIO. So the create carries no `|| true`; the only thing one could
swallow is a REAL failure (malformed JSON, admin denied), leaving the credential on its previous
policy while the hook reports success. `user add` and `attach` keep theirs. Note what `user add` does
on a re-run, because the `|| true` hides it either way: it ROTATES the existing user's secret rather
than failing (probed 2026-09-07 — afterwards the old secret gives `SignatureDoesNotMatch` and the new
one gives `Access Denied`, i.e. valid but unpolicied). That is what makes a derived secret converge.

## `"helm.sh/hook": post-install,pre-upgrade,post-upgrade`

THREE PHASES, AND EACH ANSWERS A DIFFERENT FAILURE.
`pre-upgrade` is what lets the identities be NAMED by default. With only `post-upgrade` the
MinIO user is created after the Deployments have been updated, so pods restart holding a
credential the object store has never heard of and 403 until the hook catches up — and that
window is the entire reason every scoped identity shipped defaulted to the tenant ROOT key.

`post-upgrade` STAYS, and is not a leftover: `minio-mkbucket` is deliberately not a hook
(see minio-buckets.yaml), so it lands with the release manifest and therefore runs AFTER this
pre-upgrade pass. An upgrade that adds a bucket would have a policy written before it exists,
which the store accepts and then silently governs nothing. The post pass rewrites the same
policies once the bucket is there — sound only because `mc admin policy create` overwrites,
which is probed and recorded above.

`post-install` covers the fresh install, where there is no upgrade phase to run in and no
running pod to strand.

A PRE-UPGRADE HOOK MAY ONLY CONSUME A SECRET KEY THE PREVIOUS RELEASE ALREADY PUBLISHED —
a permanent property of the phase, and a CONSTRAINT ON RENAMES rather than a reason to drop
the phase. This Job mounts the store's root credential from `infra-credentials`, which ESO
writes from the store; the mapping that names a key arrives with the release's OWN templates, which helm
applies only AFTER its pre-upgrade hooks. So an upgrade that RENAMES that key deadlocks: the
hook cannot start (`CreateContainerConfigError: couldn't find key`), helm waits for it, and
the ExternalSecret that would create the key sits in the manifest helm has not applied.
Measured on the live estate 2026-09-11 — seeding the property into OpenBao does not break it
either, and a hand-patched Secret is reverted by ESO within seconds, because ESO reconciles
continuously rather than on its refresh interval.

DROPPING THE PHASE IS THE WRONG REPAIR, and was tried: it reopens exactly the window this Job
exists to close, and `tests/unit/test_a_provisioned_identity_is_one_the_service_uses.py`
fails on it by design. RENAMING an ESO-delivered key takes TWO releases — publish the new
name alongside the old in release N, move the consumers in N+1 — because one release cannot
both introduce a key and consume it before it exists.

## `"helm.sh/hook-weight": "5"`

Ordering WITHIN a phase: after any hook that provisions buckets.

## `limits: { cpu: "200m", memory: "384Mi" }`

`mc` is killed at 128Mi: on CI's kind node its container hit `OOMKilled/137` three times part-way through the users
(XC-133, CI run 38233340359) and the install failed on the Job's backoff; the estate's runs fit under it with no margin.
384Mi is three times the size that failed.

## `ttlSecondsAfterFinished: 3600`

Reap the finished pod, exactly as `openfga-migrate.yaml` does and for the identical reason: this
Job's name carries the bootstrap revision, so the `before-hook-creation` policy above matches the
PREVIOUS revision's name never — each upgrade mints a new name and leaves the old husk holding its
completed pod. Counted live 2026-09-16: 17 of them (r146..r163). The cost is legibility, which is
the thing this estate keeps losing — a `kubectl get pods` full of husks is how four orphaned
durables and a split image stem both stayed invisible.

## `securityContext: { fsGroup: 65532 }`

Both containers run as 65532 and read the root key from a 0400 secret volume, which kubelet writes as root:root
unless the pod names an fsGroup; with one it is root:<fsGroup> and group-readable.

## `initContainers:`

EVERY SECRET IS A FILE ([[XC-004]]): the root as a mounted key of the ESO-written infra-credentials, and each
scoped secret derived from it in memory by `lance.scopedSecretFn` — the function the dev OpenBao's seed runs, so
the user `mc` creates and the field a service reads agree. In the openbao image, whose busybox sha256sum was
measured (2026-10-05). No key is in the pod spec, an env or the render.

## `image: {{ include "rask.image" (list . "minio") | quote }}`

The store's own image, which carries `mc` beside the server (.docker/minio.dockerfile).

## `volumeMounts:`

mc writes its client config under $HOME, which a read-only rootfs turns into a failure. HOME on the tmp
emptyDir is the pair every app container in this chart already uses.

## `env:`

Every secret is read from a file by `mc`, never interpolated into the rendered command, so a
`kubectl get job -o yaml` never discloses a key.

## `- { name: HOME, value: /tmp }`

HOME on the tmp emptyDir: this container now runs with a read-only rootfs, and the CLI writes under $HOME.

## `{{- $control := .Values.minio.bucket }}`

THE BUCKET SET IS NOT KNOWABLE AT RENDER TIME, so the maintenance policy enumerates none.

The sweep opens buckets that no value in this chart names. `catalog.warehouses.enabled`
(on by default) provisions one physically separate bucket per warehouse at
`POST /v1/warehouses`, with an operator-chosen name, and the sweep enumerates those buckets itself
out of the registry (`sweep.py::_buckets_to_sweep`). An allow-list built from `minio.bucket` +
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

## `mc admin policy create rfs rask-lineage /tmp/lineage.json`

THE TIGHTEST OF THE FOUR, and it is tight because the SURFACE is small — measured
2026-09-07 against the source rather than inferred from the service's importance.

NO PutObject ANYWHERE, which no other plane here can say. Lineage opens datasets
READ-ONLY to probe versions, schema and dangling blobs (`core/reconcile.py:58,76,93,108`,
`demo.py:112`), and the only bytes it changes are DELETES of outbox objects the relay has
already re-ingested (`outbox.drop_event`). `outbox.stage_event` — the write half — is
called by the PRODUCERS (the catalog, maintenance) and never here, so granting this
identity a write would grant a capability it has no code to use.

THE READ IS DELIBERATELY UNSCOPED, for the reason the maintenance policy above already records:
the bucket set is not knowable at render time. Lineage reconciles whatever datasets the
graph names, across warehouses this chart does not enumerate, so a narrowed read is a
reconciler that silently stops seeing part of the estate — and a reconciler that cannot
read reports `known=False`, which publishes nothing and looks exactly like a healthy
estate. That failure mode is why the read stays wide and the WRITE is what gets scoped.

## `mc admin policy create rfs rask-catalog /tmp/catalog.json`

WIDE ON DATA, AND THAT IS THE POINT — what it drops is ADMIN.

This identity exists to stop the catalog presenting the store's ROOT account, which it
did until LH-133 made a scoped vend possible at all: measured 2026-09-11, RustFS gates
`AssumeRole` to root and cannot grant the right, so the root key was a REQUIREMENT of
`vending.mode: sts` rather than an oversight. On MinIO a policy-attached user both
assumes AND has the session policy enforced, so the requirement is gone.

The DATA grant cannot be narrowed by bucket: warehouse buckets are minted at RUNTIME by
`POST /v1/warehouses`, so a render-time list is stale by construction — the same reason
recorded above for the maintenance and lineage policies. `s3:CreateBucket`/`s3:DeleteBucket` are here because provisioning and purge
are this service's own doors, and no other identity in the estate holds them.

WHAT ROOT HAD AND THIS DOES NOT: the admin API. No `mc admin user`, no
`mc admin policy`, no service-account minting — so a compromised catalog cannot create
an identity, cannot widen its OWN policy, and cannot read the policies governing every
other plane. That is the blast-radius change; a credential that can rewrite the policy
that governs it is root wearing another name, which is exactly what
`minio-scoped-users`' own header warns about.

## `mc admin policy create rfs rask-viewer /tmp/viewer.json`

THE TIGHTEST POLICY IN THE ESTATE, and it can be: the object browser has no write path
at all. `viewer/api/v1/endpoints/objects.py` calls `list_objects_v2`, `head_object` and
`get_object`, and nothing else — so this identity needs no `PutObject` and no
`DeleteObject`, which not even lineage's (one scoped delete on its own outbox) can say.

THE READ STAYS WIDE ON PURPOSE. A browser that cannot list a bucket does not report a
permission error to the operator — it renders the bucket as EMPTY, which is the same
failure the store-registry fix already paid for once (`_client_for`: a bucket holding
millions of objects rendered as empty, and nothing errored). Narrowing the read here
would buy little and reintroduce the least debuggable failure this plane has.

## `{{- range $ref, $locs := $fixture }}`

values.yaml `catalog.multibase.devSeed`: one user per credential reference, reaching only its bases.
