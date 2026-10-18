# The rustfs fixture

NOT FOR PRODUCTION. Rationale for `chart/templates/rustfs.yaml`, the development object store's server, which is a
fixture ([[XC-131]], ADR 0153, ADR 0168): rask's chart renders no object store and reaches this one at
`<release>-rustfs:9000`. A platform runs its own S3 and rask names it (`objectStore.externalEndpoint`). Each section is
headed by the template line it explains. How rask uses RustFS, its trust model and its traps:
`.claude/skills/rask-rustfs`.

## `required "rootCredentials ...`

With no root pair, RustFS boots on `rustfsadmin`/`rustfsadmin` and only warns. The render refuses an empty Secret name,
and the secret volume below is not optional, so a missing Secret holds the pod in `ContainerCreating` instead of
booting a store anyone can administer.

## `$labels`

The labels are LOAD-BEARING: rask's NetworkPolicy admits the data plane to pods carrying
`app.kubernetes.io/component: rustfs`, and its snapshot backup selects the claims by the same component. Renaming the
component without moving those selectors silently stops backing up the estate's data while every backup run still
reports success.

## `kind: Service`

The S3 endpoint every consumer resolves (`lance.objectStoreHost`). It publishes S3 only: the console is off and has
no port. The NodePort lets a client off the cluster reach the store at the address the catalog vends (LH-489).
RustFS serves S3, its admin API and internode RPC on this one listener, so no NetworkPolicy can split them by port: the
admin plane is protected by IAM alone, and no rask identity holds an `admin:*` action.

## `clusterIP: None`

The headless Service a StatefulSet names as its `serviceName`; one replica needs no peer DNS.

## `kind: StatefulSet`

A StatefulSet, not the official RustFS chart or its operator. The chart passes the root pair only as env, names its
Service `<fullname>-svc`, always publishes the console port and defaults to a distributed 16-claim topology; the
operator is pre-release and would be its own release. A StatefulSet's `volumeClaimTemplates` keeps the claim across
pod rolls and `helm uninstall`, because helm never owned it.

## `replicas: 1`

One server, one drive (`RUSTFS_VOLUMES=/data`), erasure coding EC:0: no redundancy. A fixture's durability is not
rask's to engineer. RustFS refuses several local paths on one device unless a bypass is set, and the bypass stays
unset. A single-drive store can never be expanded; leaving it is an S3 copy.

## `securityContext:`

uid/gid 10001 is the image's own `rustfs` user, which owns the data. A volume it cannot write stops the server at boot
with `Permission denied (os error 13)` (measured 2026-10-18, `.dagger/storage.go`), so `fsGroup` is pinned with the
uid rather than left to the image.

## `image:`

The upstream image at the GA tag the Dagger lanes run, by digest: RustFS publishes no signature, so the digest is what
makes the fixture's server the measured one. 1.0.0-alpha.60 refused pyarrow 25's multipart upload, the catalog's
record-store writer; a bump re-runs rask's own writer.

## `RUSTFS_ACCESS_KEY_FILE`

The root pair as files from `rask-object-store-root` (`object-store-access-key`, `object-store-secret-key`), which
ESO writes from rask's dev store (`secret/object-store-root`, which no Dapr app may read): never `env`, where it would be readable in `kubectl get statefulset -o yaml` and
`helm get manifest`. Only the `_FILE` pair is set; each is in a clap group with its inline form. The image's
entrypoint reads each file's first line and refuses an empty or unreadable one. The root secret also signs every STS
session token, so only this pod and rask's provisioning hook read the Secret.

## `RUSTFS_CONSOLE_ENABLE`

The console is on by default and the image sets its CORS to `*`: off. `RUSTFS_CHECK_UPDATE=false` stops the startup
call to `version.rustfs.com`. `RUSTFS_OBS_LOG_DIRECTORY=""` sends logs to stdout, where `kubectl logs` and the
Collector's filelog receiver read them (the image writes to `/logs`). Profiling export is off.

## `{{- with .Values.identityProvider }}`

The store's one identity provider is the cluster's ServiceAccount issuer ([[XC-083]], ADR 0170): the catalog holds no
store key, and STS `AssumeRoleWithWebIdentity` exchanges its projected token for its sessions and every vend's. k3s
serves its discovery and keys only to a bearer, so the store reads them from rask's issuer mirror
(`<release>-issuer-mirror:8080`, `chart/templates/issuer-mirror.yaml`), named as `configUrl` with the cluster's issuer
explicit, which RustFS requires the discovered `issuer` to equal (`rustfs@1.0.3:crates/iam/src/oidc/provider.rs:498-565`).
`HIDE_FROM_UI` makes it a workload provider, one with no browser login (`provider.rs:128-147`). The mirror is a
private address, so its exact origin is the store's one outbound allowance (`RUSTFS_OUTBOUND_ALLOW_ORIGINS`,
`rustfs@1.0.3:crates/utils/src/egress.rs:25-29`).

THE POLICY IS CHOSEN BY THE TOKEN'S SUBJECT, NEVER BY A ROLE POLICY. `CLAIM_NAME=sub` with no `ROLE_POLICY` grants a
verified token the canned policy its `sub` names, and refuses one whose `sub` names none
(`rustfs@1.0.3:crates/iam/src/federation/mapper.rs:257-281`; `rustfs@1.0.3:rustfs/src/admin/service/federated_identity.rs:332-410`),
so only an account rask's scoped-users hook writes a policy for is exchanged. A role policy would be granted to every
token the issuer signs for the audience, which any pod in the cluster can project. Measured on 1.0.3 against a
stand-in issuer (`docs/audits/2026-10-18/rustfs_web_identity_measure.py`): the catalog's subject is exchanged and its
session policy enforced; another account's token, another audience, an expired, a forged and another issuer's token are
each refused. A provider whose discovery is unreachable at boot is recovered in the background, so the store starts
before rask's mirror does (`rustfs@1.0.3:crates/iam/src/oidc/runtime.rs:151-197`).

## `livenessProbe:`

`/health/live` answers during startup, before the store serves; `/health/ready` answers once the store takes S3
requests (read quorum, which on one drive is write quorum too). A "store is back" check reads readiness.

## `name: tmp`

A bounded `emptyDir`: an unbounded one draws from the node's ephemeral storage, so a runaway write evicts every pod on
the node rather than only this one. Whether RustFS writes to `/tmp` is not measured.

## `storageClassName: {{ . | quote }}`

`with`, not a bare quote: `storageClassName: ""` DISABLES dynamic provisioning, so an empty value renders no field
and the cluster-default StorageClass applies.
