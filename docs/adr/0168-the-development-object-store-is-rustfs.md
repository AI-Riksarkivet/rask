# 0168. The development and CI object store is RustFS; MinIO leaves the repo (2026-10-18)

**Status:** accepted. Supersedes [ADR 0063](0063-the-object-store-is-minio-and-the-reason-is-who-may-call.md).
Decision 2 is amended by [ADR 0170](0170-the-catalogs-store-identity-is-its-serviceaccount.md): the development store
trusts the cluster's ServiceAccount issuer, and the catalog's store identity is its token.
Source: owner, 2026-10-18, ruling on XC-163 (#657): "migrate completely to RustFS and remove all MinIO stuff
everywhere", and "Bump to newest" (1.0.3). Evidence: `docs/audits/2026-10-18/rustfs-zero-trust.md` (requirements
R1-R20 and a live measurement on RustFS 1.0.1 and 1.0.3).

## Context

ADR 0063 (2026-09-11) moved the development store from RustFS to MinIO on one measurement: a policy-scoped user's STS
`AssumeRole` answered 403 on RustFS while root was accepted, and on MinIO the scoped user could assume and its session
policy was enforced. Under `vending.mode: sts` that made the catalog's root key a requirement of the mode, so MinIO
won. The ADR names no RustFS version; the repo pinned `rustfs/rustfs:1.0.0-alpha.60` at the time, a build published
2025-09-25, a year before GA (audit § "Why rask left RustFS").

Four facts have changed since:

- **RustFS is GA.** 1.0.0 shipped 2026-09-16, and 1.0.1 through 1.0.3 followed by 2026-10-17. Its AssumeRole handler
  refuses only temporary and service-account callers and evaluates `sts:AssumeRole` deny-only, so a scoped user with
  a resolving policy may call it and needs no `sts:` grant (audit R1). Measured 2026-10-18 on 1.0.1 and on 1.0.3, each a
  fresh store: the scoped user's `AssumeRole` with a session policy issues credentials, the session cannot read outside
  its prefix, write on a read session, or widen its parent; `If-None-Match: *` and `If-Match` answer 412 with one winner
  among 16 racing writers in each of 20 rounds; Lance creates and appends through a vended session credential and is
  refused outside it; pyarrow 25's multipart record write on a service's own scoped pair is accepted, and a
  prefix-scoped session is refused pyarrow's `HeadBucket` (audit § "Measured on RustFS", 26 checks on each).
- **MinIO's community edition is archived.** `minio/minio` and `minio/mc` are archived, the community edition ships
  as source only, and no community source carries the 2026 security fixes (audit § "MinIO's status").
- **rask's MinIO predates GHSA-jjjj-jwhf-8rgr.** That advisory lets an STS credential with a restricted session policy
  mint a service account holding its parent's full privileges. rask hands session-scoped credentials to any
  authenticated client, and their parent is the catalog user, which holds the whole data plane: the advisory's shape.
  The fixture ran `RELEASE.2025-04-22T22-12-26Z`, before the fix. RustFS requires an explicit Allow on
  `admin:CreateServiceAccount`, which no rask policy grants.
- **The CI lanes already ran RustFS.** The Dagger lanes (the storage smoke, `rustfs-lifecycle`, the governed lanes)
  pulled `rustfs/rustfs:1.0.1`, while the kind lane and the dev estate ran MinIO, so STS vending was proven on one
  store and the rest of CI on another.

## Decision

1. **The development store is RustFS, from a local fixture chart.** `deploy/fixtures/rustfs` (release `rustfs`, rask's
   own namespace) renders one StatefulSet replica `rask-rustfs`, one claim, and a Service `rask-rustfs` exposing S3 on
   9000 only, with the dev NodePort 30900 for the vended endpoint. It runs the upstream `rustfs/rustfs` image at
   `1.0.3`, the newest release, by the multi-arch index digest `.dagger/storage.go` also pins, so every lane runs the
   server the estate runs. The root pair reaches the server as files
   (`RUSTFS_ACCESS_KEY_FILE`, `RUSTFS_SECRET_KEY_FILE`) from a non-optional Secret volume, never env; the console and
   the update check are off, logs go to stdout.
2. **The product chart's store block is vendor-neutral.** `objectStore.*` (enabled, the per-plane access keys,
   buckets, `externalEndpoint`) names whatever S3 the deployment uses: the RustFS fixture in development, the
   platform's store in production. The root pair's Secret keys are `object-store-access-key` and
   `object-store-secret-key`. `minio.oidc` has no successor: the RustFS fixture does not trust rask's issuer, since
   web identity is unmeasured against it (R18), so `vending.mode: web_identity` needs a platform store configured
   for the issuer by its owner.
3. **One hook drives RustFS's native admin API from rask's own image.** `object-store-scoped-users` runs the
   rest-catalog image (python, boto3, botocore), makes the platform buckets, and calls
   `/rustfs/admin/v3/add-canned-policy`, `add-user` and `set-user-or-group-policy` with SigV4 and plain JSON bodies,
   the shape the 2026-10-18 measurement drove. It creates every policy before it binds anyone and exits non-zero on a
   policy the store refuses. No MinIO server or `mc` image is built, pinned, pushed or scanned.
4. **Every CI lane runs RustFS**: the Dagger lanes as a service bound to one exec, and the kind lane through the
   fixture.

## Considered options

- **The official RustFS Helm chart.** Rejected: it passes the root credential only as env (`envFrom` on the Secret),
  names its client Service `<fullname>-svc` with no value to change it, always publishes the console port (and a
  console NodePort in NodePort mode), and defaults to a distributed 4×4 topology with an ingress for the console. Each
  would be an override against the chart, and the env-only credential cannot be overridden at all
  (`.claude/skills/rask-rustfs/references/deployment.md` § "Why not the official chart").
- **The RustFS operator.** Rejected: v0.1.0 is a pre-release, and an operator is its own release by rule (ADR 0153), so
  it would add a CRD set to install and keep in step for a one-replica fixture.
- **Keeping MinIO.** Rejected: its only recorded advantage was a scoped user's `AssumeRole`, which RustFS GA now
  answers; its upstream is archived and source-only, rask's pin carries a published session-policy bypass, and the
  first-party build of `minio` and `mc` existed only because the upstream images refuse anonymous pulls.

## Consequences

- **The root secret signs STS tokens.** On RustFS anyone holding the root secret can forge any session token
  (GHSA-m77q-r63m-pj89, intentionally unfixed), and rotating root invalidates every outstanding STS credential. The
  root pair is a trust root, so it reaches the store pod, the dev secret store that seeds it, and the scoped-users hook,
  never a service: the dev store holds it at `secret/object-store-root`, which ESO's policy names and every Dapr app's
  Configuration denies, ESO writes it into a Secret of its own (`<release>-object-store-root`), and no other workload
  mounts that Secret (`tests/unit/test_the_root_storage_secret_reaches_only_its_users.py` follows every path a secret
  takes, the Dapr store included). Every plane presents its own scoped identity: the catalog signs `AssumeRole` with
  its own user, annotator and search read the media corpus as `objectStore.mediaAccessKey` (read-only on
  `explorer.corpusS3Root`), the backups write `_backups/` as `objectStore.backupAccessKey`, and the hook makes the
  platform buckets itself. A blank plane identity fails the render, so no plane falls back to the root.
- **The dev secret store bounds that reach.** The dev OpenBao runs `server -dev` with a published root token, which the
  Dapr secret store presents, and the dev estate runs without NetworkPolicies, so a pod that calls OpenBao directly
  reads every secret it holds, the store's root included: the Configurations' deny lists bind daprd, not the app
  container beside it, and Dapr's Vault store authenticates only by token. Per-app secret-store identities (one Vault
  Component per app with its own OpenBao token and policy) and a generated dev root token are XC-165 (#659), by the
  owner's ruling of 2026-10-18. Production is outside it: a real deployment refuses the dev store, so rask's secret
  store holds no store root there.
- **Production names the platform's S3.** A real deployment refuses `objectStore.enabled` (the dev store), and the
  render refuses an empty `objectStore.externalEndpoint`; rask provisions nothing on the platform's store, whose
  operator holds the root and creates rask's buckets and identities.
- **Rotation is unchanged.** RustFS also refuses `AssumeRole` from a service account, so ADR 0166's rotated catalog
  credential stays a second user under the same policy.
- **The estate needs a from-empty rebuild.** The store's bytes, Service name, claim, labels and Secret keys all
  change, and the estate is pre-production, so nothing is migrated: the dev estate is reinstalled from empty.
- **Open measurements.** Conditional `CompleteMultipartUpload` (R11) is read in source only; the catalog's warehouse
  purge, `DeleteObjects` then `DeleteBucket` (R13), is not measured; `AssumeRoleWithWebIdentity` (R18) is read in
  source only, and whether a Kubernetes projected ServiceAccount token serves as the web identity is open. The
  2,048-byte session-policy boundary (R5) and conditional writes across nodes on a multi-node store are unmeasured.
- **The store's hook converges under Helm and Flux, not yet under Argo CD.** `object-store-scoped-users` is named by
  the release revision, so a `helm template` render runs it once; continuous reconciliation of the store's users and
  policies is the planned operator's (XC-164, #658, parked).
- **The platform store is named by requirement, not product.** The audit's list (§ "What rask must demand of ANY
  platform store") and `rask-rustfs` § 8 are what a production store must meet; RustFS is the fixture that proves the
  list in development and CI.
