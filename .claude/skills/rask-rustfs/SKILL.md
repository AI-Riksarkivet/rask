---
name: rask-rustfs
description: "RustFS as rask's object store: the development and CI fixture (production S3 is the platform's), the zero-trust model rask depends on (the catalog's ServiceAccount token as its only store identity through AssumeRoleWithWebIdentity, a canned policy named by the token's subject, session policies, scoped users for the other planes, the provisioning hook that prunes what git does not declare), the S3 behaviours Lance and the catalog need and their measured status, how scoped users and policies are provisioned through RustFS's admin API, and the traps (uid 10001 volumes, path-style, a pre-GA image refusing pyarrow multipart, default root credentials, the console and update check on by default, file-only logs in the image). Use whenever you deploy, configure, secure, test, upgrade or debug RustFS; touch deploy/fixtures/<store>, the scoped-users hook, .dagger/storage.go or the kind lane's store; write a vending or CAS test against a real store; state what rask demands of a platform S3 store; or reach for a RustFS docs page or the official RustFS Helm chart."
---

# rask × RustFS — the store fixture, its trust model, and what the docs get wrong

RustFS is an Apache-2.0, S3-compatible object store written in Rust (`rustfs@1.0.3:LICENSE:1-3`). For rask it is
**the bundled development and CI store** (ADR 0168): the k3s fixture, the kind lane and the Dagger lanes all run
`rustfs/rustfs:1.0.3` by its index digest (`.dagger/storage.go`, `deploy/fixtures/rustfs/chart/values.yaml`). In
production, S3 is a platform service: rask names the endpoint and mints no trust root for it (CLAUDE.md, "Platform
services are external in production"). So this skill has two audiences: the fixture rask runs, and the requirement
list rask hands any platform store (§8).

Every factual line cites RustFS source as `rustfs@1.0.3:<path>:<line>` (tag `1.0.3`, commit
`331fb896285a7686f927baf8d9d4dc2d3d3a10a9`, 2026-10-17: the server the fixture and the lanes run), a docs page as
`docs:<path under content/en>`, or a rask file by its repo path. **Where the docs and the source disagree, the source
wins**, and the contradiction is listed in the reference that covers it. The evidence for rask's own requirements
(R1-R20) is `docs/audits/2026-10-18/rustfs-zero-trust.md`, which also holds the live measurement on 1.0.1 and 1.0.3.

## 1. Read the source, not the integration pages

- The integration pages say they were verified against `rustfs/rustfs-x86-musl:v2.3.1`. No such image or version
  exists in the source: the workspace is 1.0.3 (`rustfs@1.0.3:Cargo.toml:77`), the in-tree chart 1.0.1
  (`rustfs@1.0.3:helm/rustfs/Chart.yaml:5-6`), and CI publishes only `rustfs/rustfs`, `ghcr.io/rustfs/rustfs` and
  `quay.io/rustfs/rustfs` (`rustfs@1.0.3:.github/workflows/docker.yml:67-70`). Treat every "verified with" claim on
  those pages as unverified.
- No docs page covers Lance with STS-vended credentials, session tokens, conditional commits or concurrent writers
  (`docs:developer/integration/database/lancedb.md` uses the root pair and says it is for local testing). rask's own
  live measurement and its e2e lanes are the only evidence for the Lance path.
- RustFS is S3-compatible, not S3-identical. Its own compatibility gate is the Ceph s3-tests lists under
  `rustfs@1.0.3:scripts/s3-tests/`; cite those files at the pinned tag, not the docs snapshot
  (`docs:reference/s3-compatibility.md` counts 455/17/270; the source has 460/21/274).

## 2. What rask relies on, and its measured status

Measured live on `rustfs/rustfs:1.0.3` (the pin) and `1.0.1`, each a fresh single node, 2026-10-18 (audit § "Measured
on RustFS", `docs/audits/2026-10-18/rustfs_sts_measure.py`): 26 checks, all as rask needs them, on both. Summary
(details and the full list in `references/s3-compat.md`):

| Need | Status |
|---|---|
| A non-root, policy-scoped user calls STS `AssumeRole` with an inline session policy (R1; no rask caller since ADR 0170) | measured |
| The session policy is intersected with the parent's on every request; a session cannot widen its parent (R2) | measured |
| An explicit Deny on `sts:AssumeRole` refuses the user's AssumeRole; `s3:prefix` `StringLike` gates ListBucket (R4) | measured |
| A session policy's explicit Deny beats an Allow (R3, rask's `NeverAFinalManifest`) | source only (`rustfs@1.0.3:crates/policy/src/policy/policy.rs:70-76`) |
| `DurationSeconds=900` honoured (R6) | measured |
| `If-None-Match: *` → 412 for the loser; 16 racing writers × 20 rounds, one winner each round (R9) | measured |
| `If-Match` with a stale ETag → 412 (R10) | measured |
| Lance creates and appends through a vended session credential; writing outside the prefix is refused (R15) | measured |
| pyarrow 25 multipart (the record store, on a service's own scoped pair) (R12) | measured on 1.0.1 and 1.0.3; refused by 1.0.0-alpha.60 |
| A prefix-scoped session is refused pyarrow's `HeadBucket` (an unprefixed `s3:ListBucket`) | measured (`rustfs@1.0.3:rustfs/src/storage/access.rs:2955-2960`) |
| Conditional CompleteMultipartUpload (R11) | source only (`rustfs@1.0.3:crates/ecstore/src/set_disk/ops/multipart.rs:2702-2742`) |
| Warehouse purge: DeleteObjects then DeleteBucket (R13) | not measured |
| Session policy at the 2,048-byte boundary (R5) | source only (`rustfs@1.0.3:rustfs/src/admin/service/session_policy.rs:34-39`) |
| AssumeRoleWithWebIdentity for a Kubernetes-shaped token, granted the canned policy its `sub` names; another account's, another audience's, an expired, forged or other-issuer token refused (R18) | measured on 1.0.3 with a stand-in issuer, 20 of 20 (`docs/audits/2026-10-18/rustfs_web_identity_measure.py`) |
| Conditional writes exclusive across nodes (multi-node) | source only: the precondition is re-checked under a namespace write lock that, on a distributed store, needs a majority of the set's lockers (`rustfs@1.0.3:crates/ecstore/src/set_disk/ops/locking.rs:46-61`); not measured |

## 3. The zero-trust model rask relies on

- **The catalog's store identity is its ServiceAccount token, and it holds no key** (ADR 0170). Its pod projects a token
  for `objectStore.identity.audience` (`rask-object-store`), and unsigned STS `AssumeRoleWithWebIdentity` exchanges it
  for every session the catalog signs with: its own IO's (an hour, renewed at half life,
  `service_kit.governed.store_session`) and each vend's (900 s, narrowed by the per-table session policy, named for the
  caller). The fixture's provider (§5) sets `CLAIM_NAME=sub` and no role policy, so a verified token is granted the
  canned policy its `sub` names (`rustfs@1.0.3:crates/iam/src/federation/mapper.rs:257-298`), and the hook writes the
  catalog's under `system:serviceaccount:<ns>:<release>-sa-catalog` with no user bound to it. A `sub` naming no
  policy is refused 400 `InvalidRequest` (`rustfs@1.0.3:rustfs/src/admin/service/federated_identity.rs:322-440`), so
  another pod's token for the same audience gets nothing; a token failing verification is refused 403. A session is
  allowed only where its session policy and the claim-named policy both allow (`rustfs@1.0.3:crates/iam/src/sys.rs:1532-1665`),
  and the policy is resolved again on every request: deleting it cuts every live session off at once.
- **Never vend from root.** A credential whose parent is root counts as owner and can bypass a bucket-policy Deny on
  Get/Put/DeleteBucketPolicy (`rustfs@1.0.3:rustfs/src/storage/access.rs:1220-1231`), and owner semantics bypass
  identity policy (`rustfs@1.0.3:crates/policy/src/policy/policy.rs:57-96`). The session policy still bounds a root
  session (`sys.rs:1450-1458`), but rask keeps root in the provisioning hook only (R17).
- **STS tokens are signed with the ROOT secret.** Advisory GHSA-m77q-r63m-pj89 is intentionally unfixed: anyone holding
  the root secret can forge any session token, and rotating root invalidates every outstanding STS credential
  (`rustfs@1.0.3:crates/iam/src/root_credentials.rs:25-36`). The root pair is a trust root, not just an admin
  login: only the store pod, the dev secret store that seeds it and the provisioning hook read it. It sits at
  `secret/object-store-root`, which only ESO's store policy reads, and in a Secret of its own,
  `<release>-object-store-root`; `tests/unit/test_every_secret_in_the_dev_store_has_exactly_its_readers.py` follows every
  path the chart hands a secret through. Each plane presents a scoped identity instead, the media readers and the
  backups included, and a blank one fails the render. The dev OpenBao publishes no token: each app's sidecar logs in as
  its own ServiceAccount and reads only its own secrets, and the store's administrator token is generated in-cluster and
  held by its seed (ADR 0169).
- **Session duration is 900–43,200 s**, default 3,600; a shorter request is clamped UP to 900
  (`sts.rs:64-83`). rask vends at 900 (`vending.ttlSeconds`, `chart/values.yaml`). rask cannot vend a credential shorter than 15 minutes.
- **Session policies**: `Version` must be present and `2012-10-17` (`session_policy.rs:30-32`;
  `rustfs@1.0.3:crates/policy/src/policy/policy.rs:25,170`), at most 2,048 bytes after re-serialisation
  (`session_policy.rs:34-39`). A canned policy needs `Version` too: the admin API refuses one without it
  (`policies.rs:229-231`), so `scripts/rustfs_admin.py` parses every policy before its first write.
- **A session cannot mint an escape hatch.** Creating a service account needs an explicit Allow on
  `admin:CreateServiceAccount`, never deny-only, and a temporary credential also needs its session policy to allow it
  (`rustfs@1.0.3:rustfs/src/admin/handlers/service_account.rs:362-390`). This closes the shape of MinIO's
  GHSA-jjjj-jwhf-8rgr. Keep every `admin:*` action out of every rask policy.
- **`s3:*` does not grant force-delete.** `s3:ForceDeleteBucket`/`s3:ForceDeleteObject` must be named explicitly
  (`rustfs@1.0.3:crates/policy/src/policy/action.rs:184-198`). The catalog's purge works with `s3:DeleteBucket`
  alone; never name the force actions in a rask policy.
- **Rotation is a second USER** (ADR 0166) for the planes that still hold a pair (lineage, maintenance). A multibase
  data base holds none: it is its tables' storage, reached under the catalog's own identity ([[LH-286]]). AssumeRole refuses a service account (`sts.rs:219-221`), so a pair that signed AssumeRole
  would rotate to another user under the same policy; no rask pair signs AssumeRole any more.
- **Never set `RUSTFS_IDENTITY_OPENID_ROLE_POLICY`.** With it, **every** valid token for the audience gets the whole
  role policy, and any pod may project a token for any audience as its own ServiceAccount
  (`rustfs@1.0.3:crates/iam/src/federation/mapper.rs:257-298`). Without it the token's `groups` claim is mapped to
  policies too (`OIDC_DEFAULT_GROUPS_CLAIM`, `rustfs@1.0.3:crates/config/src/constants/oidc.rs:102`); a ServiceAccount
  token carries none. `tests/unit/test_a_provisioned_identity_is_one_the_service_uses.py` fails a fixture render that
  sets a role policy. See `references/security.md` § Web identity.
- **There is one listener for S3, admin and internode RPC** (`:9000`): the admin API (`/rustfs/admin`, alias
  `/minio/admin`), the Iceberg REST catalog (`/iceberg/v1`, `/_iceberg/v1`) and peer RPC share it
  (`rustfs@1.0.3:rustfs/src/server/prefix.rs:51-73`; `rustfs@1.0.3:rustfs/src/server/hybrid.rs:35`). A
  NetworkPolicy cannot split them by port, so the admin plane is protected by IAM alone: no `admin:*` on any rask
  identity, root out of every service.

## 4. Provisioning users and policies

rask's runtime makes no admin call (R17). Users, policies and buckets are made once, by a hook, with the root pair.
**Use RustFS's native admin API with SigV4 and plain JSON** — the 2026-10-18 measurement drove exactly these
(audit § Measured):

| Step | Call | Source |
|---|---|---|
| canned policy | `PUT /rustfs/admin/v3/add-canned-policy?name=<p>`, body = the policy JSON | `rustfs@1.0.3:rustfs/src/admin/handlers/policies.rs:71` |
| user | `PUT /rustfs/admin/v3/add-user?accessKey=<ak>`, body `{"secretKey":…,"status":"enabled"}` | `rustfs@1.0.3:rustfs/src/admin/handlers/user_lifecycle.rs:44` |
| bind (replaces) | `PUT /rustfs/admin/v3/set-user-or-group-policy?policyName=<p>&userOrGroup=<ak>&isGroup=false` | `policies.rs:83` |
| bind (adds) | `POST /rustfs/admin/v3/idp/builtin/policy/attach` | `policies.rs:93` |
| bucket | S3 `CreateBucket` (boto3) | S3 API |
| list users | `GET /rustfs/admin/v3/list-users`: plain JSON keyed by access key, regular users only (no session, no service account) | `user_lifecycle.rs:26`; `rustfs@1.0.3:crates/iam/src/manager.rs:1367-1377` |
| remove user | `DELETE /rustfs/admin/v3/remove-user?accessKey=<ak>`: 404 once absent | `user_lifecycle.rs:38` |
| list / remove policy | `GET /rustfs/admin/v3/list-canned-policies`; `DELETE /rustfs/admin/v3/remove-canned-policy?name=<p>` | `policies.rs:59,77` |

THE STORE'S IDENTITIES ARE WHAT GIT DECLARES (ADR 0170). The hook's install run and its post-upgrade run
(`<release>-object-store-scoped-users-prune-<rev>`) pass `--prune`: every regular user the run does not declare is
removed (the root aside), then every canned policy rask owns (`rask-*`, or named after a ServiceAccount,
`scripts/rustfs_admin.py` `OWNED_POLICY_PREFIXES`) that it does not create; a policy rask does not own is never
touched. The pre-upgrade run prunes nothing, because the previous release's pods still present what it would remove. A
user or policy made by hand on the dev store is therefore gone at the next upgrade. Pinned against a real RustFS by
`tests/e2e-py/test_a_policy_the_store_refuses_fails_the_scoped_users_hook.py`.

Rules: the policy before the binding; a user name must not equal the root access key; access keys alphanumeric (a `/`
breaks SigV4 credential scope, `docs:operations/credentials.md`); bucket names match
`^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$` and `rustfs` is reserved (`rustfs@1.0.3:crates/ecstore/src/bucket/utils.rs:26-65`).
Do not build on `rc`: its reference page documents no user or policy command (`docs:operations/rc.mdx`); only the
site-replication page shows `rc admin user add` and `rc admin policy attach`, and no page shows creating a canned
policy (`docs:operations/high-availability/site-replication.md:146-152`). The `rc` source is not in the clone, and the
integration pages run it as `rustfs/rc:latest`, a mutable tag (`docs:developer/integration/observability/loki.md:111`). `mc` through `/minio/admin` is unmeasured and its upstream is archived (audit § MinIO's status).
Details in `references/security.md` § Provisioning.

## 5. The fixture's shape

The fixture is **a local chart** (`deploy/fixtures/rustfs/chart`, `CHART=chart`, `WAIT=false`,
`SHARED_NAMESPACE=true`), rask's own, not the official chart. Why, and every value it sets, is in
`references/deployment.md`. The short form:

- One StatefulSet replica, one PVC at `/data`, `RUSTFS_VOLUMES=/data` (single drive, EC:0, no redundancy:
  `rustfs@1.0.3:crates/ecstore/src/config/storageclass.rs:24-32`). A fixture's durability is not rask's to engineer.
- Root pair from rask's ESO-written Secret `<release>-object-store-root`, **mounted as files**: `RUSTFS_ACCESS_KEY_FILE` /
  `RUSTFS_SECRET_KEY_FILE` (`rustfs@1.0.3:rustfs/src/config/cli.rs:1551-1564`), never env. A non-optional secret
  volume, so a missing Secret blocks the pod instead of booting on defaults.
- Pod: `runAsUser`/`runAsGroup`/`fsGroup` 10001, `runAsNonRoot`, `readOnlyRootFilesystem`, drop ALL,
  `automountServiceAccountToken: false` (`rustfs@1.0.3:Dockerfile:107-125`).
- Probes on `:9000`: liveness `/health/live`, readiness `/health/ready`
  (`rustfs@1.0.3:rustfs/src/server/prefix.rs:30-49`).
- Env: `RUSTFS_CONSOLE_ENABLE=false`, `RUSTFS_CHECK_UPDATE=false`, `RUSTFS_HEALTH_MINIMAL_RESPONSE_ENABLE=true`,
  `RUSTFS_OBS_LOG_DIRECTORY=""`, `RUSTFS_OBS_PROFILING_EXPORT_ENABLED=false`; `RUSTFS_DURABILITY_MODE` unset (strict).
  Each with its source line in `references/config.md`.
- Service exposes S3 only (no console port), NodePort 30900 for the vended endpoint (LH-489).
- The identity provider, from `identityProvider` (ADR 0170): `RUSTFS_IDENTITY_OPENID_CONFIG_URL` at rask's issuer
  mirror (`http://rask-issuer-mirror:8080/.well-known/openid-configuration`; k3s answers discovery only to a bearer),
  `RUSTFS_IDENTITY_OPENID_ISSUER` the cluster's issuer, which the discovered `issuer` must equal
  (`rustfs@1.0.3:crates/iam/src/oidc/provider.rs:498-588`), `CLIENT_ID` the catalog's store audience, `CLAIM_NAME=sub`,
  `HIDE_FROM_UI=true` (a discovery document with no `authorization_endpoint` is a workload provider only then,
  `provider.rs:128-153`), and `RUSTFS_OUTBOUND_ALLOW_ORIGINS` the mirror's origin, the one private origin the store's
  egress guard admits (`rustfs@1.0.3:crates/utils/src/egress.rs:25-29`). No role policy (§3). The settings live in the
  pod template, so a change rolls the store.

## 6. Traps (each measured or read in source)

- **Default root credentials boot silently.** With no root pair, the server falls back to `rustfsadmin`/`rustfsadmin`
  and only warns (`rustfs@1.0.3:crates/credentials/src/constants.rs:21,28`;
  `rustfs@1.0.3:rustfs/src/startup_server.rs:100-109`). RustFS will not refuse; the fixture must (non-optional
  secret volume, and a render-time `required`).
- **A volume the image's user cannot write stops the server**: `Permission denied (os error 13)` on a root-owned mount
  (measured 2026-10-18, `.dagger/storage.go:17-20`). uid/gid 10001, `fsGroup: 10001`.
- **Path-style only.** Without `RUSTFS_SERVER_DOMAINS` only path-style addressing works
  (`rustfs@1.0.3:rustfs/src/config/cli.rs:1535-1548`). rask's clients are path-style by default
  (`packages/service-kit/src/service_kit/lakehouse/objectfs.py:37,45`); keep vended options path-style too.
- **A pre-GA image refused pyarrow 25's multipart.** 1.0.0-alpha.60 answered `UploadPart` with
  `XAmzContentSHA256Mismatch`; 1.0.1 and 1.0.3 take it (`.dagger/storage.go`; `docs/agents/lessons.md:97-101`). Pin a
  GA tag, by digest, and re-run rask's own writer and `docs/audits/2026-10-18/rustfs_sts_measure.py` on every bump.
- **pyarrow cannot write through a vended credential.** Before a write, pyarrow's `S3FileSystem` asks `HeadBucket`,
  which RustFS authorizes as an unprefixed `s3:ListBucket` (`rustfs@1.0.3:rustfs/src/storage/access.rs:2955-2960`), as
  AWS does; a session scoped to a table's prefix is refused it (measured on 1.0.1 and 1.0.3). rask writes its records
  with a service's own scoped pair, and a vended credential writes through Lance's object_store, which never asks. Never
  widen a session policy to the bucket root to make pyarrow work.
- **The image logs to files, not stdout.** It sets `RUSTFS_OBS_LOG_DIRECTORY=/logs` and
  `RUSTFS_OBS_ENVIRONMENT=production` (`rustfs@1.0.3:Dockerfile:115-119`), and in production the stdout mirror is off
  (`rustfs@1.0.3:crates/obs/src/telemetry/local.rs:78-80`). Set the directory to empty
  (`rustfs@1.0.3:crates/obs/src/config.rs:286-293`) or `kubectl logs` and the Collector's filelog see nothing.
- **The console is on by default** (`rustfs@1.0.3:crates/config/src/constants/app.rs:52`) and the image sets its
  CORS to `*` (`Dockerfile:115`). Turn the console off.
- **The store phones home at startup** to `https://version.rustfs.com/latest.json`
  (`rustfs@1.0.3:crates/config/src/constants/console.rs:93-96`; `rustfs@1.0.3:rustfs/src/update.rs:101`).
  `RUSTFS_CHECK_UPDATE=false`, the literal `false`.
- **Several local paths on one device are refused** unless a bypass is set
  (`rustfs@1.0.3:crates/ecstore/src/layout/endpoints.rs:1590-1607`). Use one volume, and never set the bypass.
- **Liveness answers before the store serves.** Liveness is local and 200 during startup, while non-probe requests get
  503 until readiness (`rustfs@1.0.3:rustfs/src/server/health.rs:108-124`;
  `rustfs@1.0.3:rustfs/src/server/readiness.rs:150-190`). A "store is back" check uses `/health/ready`, as
  `tests/e2e-py/chaos.py` does; `/health/live` and its alias `/minio/health/live`
  (`rustfs@1.0.3:rustfs/src/server/layer.rs:1441`) can pass while every S3 request still gets 503.
- **Readiness is read quorum, not write quorum** (`readiness.rs:867-886`). On one drive they coincide; on a multi-node
  store a Ready pod may not accept writes. A resilience drill proves writes with a real conditional PUT.
- **A fresh estate refuses every compaction until maintenance has swept once**: `version/create` judges a compaction
  against the clone list the sweep writes (ADR 0162). This is store-independent, and every fresh fixture hits it; the
  kind lane runs the first sweep itself (`scripts/e2e_stack.sh`, step 8/9).
- **No Prometheus endpoint.** RustFS pushes OTLP/HTTP only; there is no `/metrics` route
  (`rustfs@1.0.3:crates/obs/src/telemetry/otel.rs:424-480`). Point `RUSTFS_OBS_ENDPOINT` at the Collector's 4318.
- **Bucket versioning and Object Lock break Lance cleanup.** On a versioned bucket a delete only writes a marker, and a
  locked version cannot be deleted. Lance keeps its own versions; rask buckets stay unversioned and unlocked
  (`references/operations.md` § Data features).
- **Region.** The server defaults to `us-east-1` (`rustfs@1.0.3:crates/config/src/constants/app.rs:289-292`).
  Whether it rejects another signing region is not traced (the check lives in the `s3s` dependency).
  `AWS_REGION` in `chart/values.yaml` defaults to `us-east-1` for that reason. Sign with `us-east-1`.
- **An admin route refuses a bare `?`.** A call with an empty query string answers 400 "failed to decode query"
  (found against 1.0.3, 2026-10-18); `scripts/rustfs_admin.py` sends no `?` without parameters.
- **A refused token costs a round trip to the issuer.** A token that fails verification makes the store fetch discovery
  and the key set again, once, before it refuses (`rustfs@1.0.3:crates/iam/src/oidc/runtime.rs:740-798`): on the estate
  a request to the issuer mirror and one from it to the API server.
- **A provider whose discovery fails at boot is recovered in the background,** backing off to 30 s
  (`rustfs@1.0.3:crates/iam/src/oidc/runtime.rs:151-216`), so a store that starts before the issuer mirror refuses every
  exchange until the mirror answers, and the catalog's boot exchange waits out a minute of it.
- **A resource's trailing `*` does not match an empty suffix.** Measured on 1.0.3 (2026-10-28, [[LH-535]]): a user
  holding maintenance's policy, with its Deny of `s3:PutObject` on `arn:aws:s3:::*/_bases/*`, is refused
  `_bases/table-x.json` and `_bases/x` and admitted the bare key `_bases/` (the same for `_projects/`). AWS's `*`
  matches zero characters, so a conformant store refuses that PUT: a test on this fixture cannot show a defect that
  only a directory marker triggers. Assert that the code writes no marker instead (pyarrow's `delete_file` PUTs the
  parent `<prefix>/` after every delete; `service_kit.lakehouse.stored_objects` deletes by key).

## 7. Testing against RustFS

Load `rask-testing` first. The proofs that matter run against a real RustFS, never moto: the CAS harness
(`tests/e2e-py/test_object_store_cas_e2e.py`), the STS/session-policy checks (`docs/audits/2026-10-18/rustfs_sts_measure.py`),
a Lance write through a vended credential, and the provisioning hook's refusal path
(`tests/e2e-py/test_a_policy_the_store_refuses_fails_the_scoped_users_hook.py`, `dagger call rustfs-provisioning`, whose
store boots from files as the fixture's does). A Dagger lane brings RustFS up as a service bound to one exec
(`.dagger/storage.go`); never `docker run` it (CLAUDE.md toolchain rule). A store-version bump re-runs the measurement
script and every RustFS lane in the same commit; the provisioning test's last assert (a refused update leaves the bound
user its old policy) can go red only on such a bump, since RustFS refuses a document before writing it and refuses
removing a bound policy (`rustfs@1.0.3:rustfs/src/admin/handlers/policies.rs:216-226`;
`rustfs@1.0.3:crates/iam/src/manager.rs:555-557`).

The CAS check rests on Lance's own requirement: a commit needs put-if-not-exists (or rename-if-not-exists) so exactly
one writer wins a manifest version (`lance_docs/lance/format/table/transaction.md` § Commit Protocol, "Storage Primitives"). The catalog
proves the same primitive, plus prefix scope, against any warehouse's store through a vended credential:
`POST /management/v1/warehouses/{warehouse_id}/validate` writes inside the prefix, is refused outside it, and sends two
`If-None-Match: *` PUTs at one key expecting the second refused
(`services/catalog/src/catalog/api/v1/endpoints/warehouses.py:104,1165-1166,1211,1274-1291`). Run it against a platform
store before rask relies on it; it is not a contention test, so the CAS harness still decides exclusivity.

## 8. What rask demands of a platform store

The audit's requirement list (§ "What rask must demand of ANY platform store") plus these, each argued in
`references/security.md` § Platform store checklist: no default root credential; root held by the platform only; an
explicit `RUSTFS_RPC_SECRET` on multi-node; TLS with a CA rask trusts; console unreachable from workload namespaces;
path-style accepted or the server domain named; durability `strict`; rask buckets unversioned (or versioned with a
noncurrent-expiry the platform owns), no Object Lock default retention, no expiration lifecycle on rask prefixes
beyond AbortIncompleteMultipartUpload; one writable site per table (async replication cannot arbitrate a Lance
commit); conditional writes proven exclusive across nodes.

## Routing

| Working on | Read |
|---|---|
| The fixture chart, the kind lane, CI, the official chart or operator, production topology | `references/deployment.md` |
| IAM, STS, web identity, TLS, KMS, audit, provisioning, hardening checklists | `references/security.md` |
| Health, probes, observability, upgrades, scaling, healing, versioning/lifecycle vs Lance | `references/operations.md` |
| Which S3 operations rask uses and their verified status | `references/s3-compat.md` |
| An env var or flag, its default and its source line | `references/config.md` |
