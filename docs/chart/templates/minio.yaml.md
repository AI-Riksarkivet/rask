# chart/templates/minio.yaml

Rationale for `chart/templates/minio.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: v1`

The ONE in-cluster object store. It is MinIO rather than RustFS for one measured reason (LH-133):
RustFS gates STS `AssumeRole` to the ROOT account and the right is not policy-grantable — a
policy-attached scoped user answers HTTP 403 while root receives a session token, and
`mc admin policy create` refuses a statement carrying `sts:AssumeRole` outright ("invalid resource,
type: 'unknown'"). Under `vending.mode: sts` that made the catalog's root key a REQUIREMENT of the
mode rather than an oversight, so the estate could not reach zero trust on it. The identical probe on
MinIO accepts the scoped user AND enforces the narrowing (cross-tenant GET and read-tier PUT both
AccessDenied), which is the whole capability the credential vending design rests on.

A STATEFULSET, NOT AN OPERATOR TENANT, and that is a deliberate departure from the P4 ruling's
mechanism while keeping its guarantee. P4 (docs/adr/0124-chart-unification-one-control-plane-each-one-object-store.md) pinned the "keep-PVC posture via Tenant spec"; a
StatefulSet's `volumeClaimTemplates` has exactly that property — its PVCs survive pod rolls AND
`helm uninstall`, because helm never owned them. What changes is that swapping backends no longer
swaps one operator subchart for another: there is no second CRD set to install, version and keep in
step with the store it manages. If a multi-node topology is ever wanted, the MinIO operator can be
introduced without touching a single consumer — every one of them resolves a Service name.

## `selector:`

The S3 endpoint every consumer resolves (`lance.s3Endpoint`). A normal ClusterIP rather than the
headless peer Service below: clients want one address load-balanced across replicas, and only the
servers themselves need to address each other individually.

## `clusterIP: None`

HEADLESS, and load-bearing rather than conventional: MinIO addresses its peers by stable DNS
(`<pod>.<headless>.<ns>.svc`), which only a headless Service publishes. `publishNotReadyAddresses`
is what lets the set form a quorum at all — every member is unready until the cluster is up, so
waiting for readiness before publishing DNS is a deadlock.

## `{{- with .Values.imagePullSecrets }}`

The estate builds this image from pinned upstream source (.docker/minio.dockerfile), so it takes the pull secrets.

## `securityContext:`

uid 1000 is the minio image's built-in `minio-user`, which owns the on-disk data. A mismatched
uid does NOT CrashLoop — the server READS fine and every WRITE fails — which is the failure
mode that took the data plane down live on 2026-07-13 under the previous store, so the uid and
the fsGroup are pinned together rather than left to the image default.

## `- server`

One server, N drives: MinIO derives its erasure set from the drive list, and unlike the
previous store it does not refuse volumes that share a physical disk — so the
`UNSAFE_BYPASS_DISK_CHECK` escape hatch single-node k3s needed has no counterpart here
and is deliberately absent rather than ported.

## `- name: MINIO_ROOT_USER`

secretKeyRef, never `value:` — a literal here is readable in `kubectl get statefulset
-o yaml`, `kubectl describe` and `helm get manifest`, which is the "same secret still
recoverable from another surface" finding.

It comes from `<release>-infra-credentials`, which the external-secrets operator writes
from the store, rather than through the Dapr secret store like the fleet's credentials: the
consumer is the object store itself, a third-party server with no daprd sidecar.

## `- name: MINIO_IDENTITY_OPENID_CONFIG_URL`

OIDC identity provider (Dex) for STS AssumeRoleWithWebIdentity — a client presents its
Dex id_token and the store mints short-TTL creds bound to ROLE_POLICY, which the
catalog's inline session policy then scopes per-table (Track B / credential vending).

## `- name: MINIO_IDENTITY_OPENID_ROLE_POLICY`

The base policy every Dex-authenticated token assumes; the per-request session policy can
only narrow it. `readwrite` is a built-in policy (full S3).

## `livenessProbe:`

`/minio/health/live` answers before the erasure set is formed, which is what a LIVENESS
probe must do — a readiness-shaped liveness check kills a node that is waiting for its
peers and the set never forms. Readiness uses the cluster-aware endpoint instead.

## `{{- include "lance.tmpVolume" . | nindent 8 }}`

The BOUNDED helper, not a bare emptyDir: an unbounded one draws from the node's ephemeral
storage, so a runaway write evicts every pod on the node rather than only this one.

## `labels:`

The labels are LOAD-BEARING — templates/backup-snapshot.yaml selects the lakehouse PVCs by
this pair, so renaming the component without updating that selector silently stops backing
up the estate's data while every backup run still reports success.

## `storageClassName: {{ . | quote }}`

`with`, not a bare quote: `storageClassName: ""` DISABLES dynamic provisioning in k8s —
an empty value must render NO field so the cluster-default StorageClass applies. A
non-existent class leaves the PVCs Pending forever with no event on the pod and no
chart-level complaint, which is how that failure was found the first time.
