# RustFS and zero trust: can rask keep RustFS, and does it need MinIO? (2026-10-18)

## The question

The owner asked: "Can rask keep RustFS as its object store, and does rask need MinIO at all?"

Concretely: can RustFS (github.com/rustfs/rustfs, GA 1.0.0 on 2026-09-16 through 1.0.3 on 2026-10-17) meet rask's
zero-trust requirements for its object store, and what would rask lose or gain by dropping MinIO?

Sources and pins:

- rask at branch `xc033-e2e-lanes`, HEAD `4667ba3a`.
- RustFS source at `main` commit **`3b0ee83466b7407ab6b21cba1cc0908127689368`** (2026-10-18, "fix(scanner): pace
  checkpoints by snapshot construction cost (#8656)"). Cited below as `rustfs@3b0ee83:<path>:<line>`. Read with
  grep/sed only; nothing was built or run.
- RustFS tags read through the GitHub contents API: `1.0.0-alpha.60`, `1.0.0`, `1.0.1`, `1.0.3`.
- RustFS docs: https://docs.rustfs.com/en/security-compliance/iam/sts (fetched 2026-10-18).
- MinIO: github.com/minio/minio README, releases, tags and security advisories through the GitHub API (2026-10-18).

Nothing in this file was measured. The main session measures RustFS live and fills its own section.

## Why rask left RustFS (its own record)

- On 2026-09-11 a policy-attached scoped user's STS AssumeRole answered HTTP 403 on RustFS, and root was accepted
  (`docs/adr/0063-the-object-store-is-minio-and-the-reason-is-who-may-call.md:6-14`).
- `mc admin policy create` refused a statement carrying `sts:AssumeRole` with "invalid resource, type: 'unknown'"
  (`docs/adr/0063-…:16-18`).
- On MinIO a scoped user could assume, and the session policy was enforced: in-scope GET allowed, cross-tenant GET
  and read-tier PUT denied (`docs/adr/0063-…:18-20`; `chart/values.yaml:1705-1709`).
- The ADR does not name the RustFS version it measured (`docs/adr/0063-…:1-25`). The catalog's own docstring says
  the repo pinned `rustfs/rustfs:1.0.0-alpha.60` when it probed STS on 2026-09-24
  (`services/catalog/src/catalog/core/vending.py:17-24`). RustFS published 1.0.0-alpha.60 on 2025-09-25 (GitHub
  release API). So the measurement that moved rask was very likely taken on a build one year older than GA. That is an
  inference: the ADR itself does not say.
- In alpha.60, the AssumeRole handler only refused temporary and service-account callers, under a
  `// TODO: Check permissions` comment, and it answered every action other than AssumeRole with "not support action"
  (`rustfs@1.0.0-alpha.60:rustfs/src/admin/handlers/sts.rs:68-87`). The 403 rask saw for a scoped user is not
  explained by that handler. Where it came from in alpha.60 is not established here.

## rask's requirements of its object store

The verdict column judges RustFS's SOURCE at `3b0ee83` (and the GA tags where stated). "Met in source" is not
"observed working". The live measurement belongs to the main session.

| # | Requirement | rask evidence | RustFS evidence | Verdict |
|---|---|---|---|---|
| R1 | A non-root, policy-scoped user can call STS `AssumeRole`. | No rask caller needs it: the catalog's vends and its own IO exchange its ServiceAccount token instead (R18, ADR 0170), and no other plane calls STS. Each scoped user's policy carries S3 actions only, no `sts:*`: `chart/templates/object-store-scoped-users.yaml`. | The handler refuses only temporary and service-account callers: `rustfs@3b0ee83:rustfs/src/admin/handlers/sts.rs:219-221`. It checks `sts:AssumeRole` with `deny_only: true`: `sts.rs:225-240`. With `deny_only`, the engine returns allow when no Deny matches: `crates/policy/src/policy/policy.rs:70-81`. The user still needs at least one bound policy that resolves, or the request is denied: `crates/iam/src/sys.rs:1488-1530`. `sts:AssumeRole` is a parseable action: `crates/policy/src/policy/action.rs:732-737`. The same gate is in tags 1.0.0, 1.0.1 and 1.0.3 (`sts.rs:219,235` at each tag). The doc page says the same: https://docs.rustfs.com/en/security-compliance/iam/sts. | **Met in source** (1.0.0 and later). Live: pending. |
| R2 | The inline session policy narrows the vended credential on every request (intersection with the parent's policy). | The policy is built per table and tier and passed as `Policy=`: `vending.py:731-734`. Its own docstring states the intersection-only premise: `vending.py:503-505`. | The handler parses and stores the policy in the session claims: `rustfs@3b0ee83:rustfs/src/admin/service/session_policy.rs:21-48`. At request time a temporary credential takes the STS path: `crates/iam/src/sys.rs:1407-1415`. The session policy is read from claims: `sys.rs:1852-1873`. The verdict is `session_ok && (is_owner \|\| parent_policy_allows)`: `sys.rs:1450-1458`. So even a session minted by root stays bounded by its session policy. A session policy that fails to parse becomes deny-all: `sys.rs:1857-1859`. | **Met in source.** Live: pending. |
| R3 | An explicit `Deny` in the session policy beats an `Allow` (rask's `NeverAFinalManifest`). | `vending.py:603`. The docstring calls this Deny load-bearing: `vending.py:491-495`. | Deny statements are evaluated before Allow: `rustfs@3b0ee83:crates/policy/src/policy/policy.rs:70-76`. A session policy is evaluated through the same `is_allowed`: `crates/iam/src/sys.rs:2001-2005`. | **Met in source.** Live: pending. |
| R4 | `s3:ListBucket` is gated by an `s3:prefix` `StringLike` condition. | `vending.py:576`, `vending.py:644`; scoped users: `chart/templates/object-store-scoped-users.yaml:179,244,276,295,310`. | `s3:prefix` is a condition key: `rustfs@3b0ee83:crates/policy/src/policy/function/key_name.rs:219-220`. The `prefix` query parameter of ListBucket, ListBucketVersions and ListBucketMultipartUploads is copied into the conditions: `rustfs/src/storage/access.rs:819-841`. `StringLike` is supported: `crates/policy/src/policy/function/condition.rs:32,69`. | **Met in source.** |
| R5 | A session policy up to rask's size fits. rask sizes write credentials against a 2,048-byte limit. | `vending.py:495-498` (measured on MinIO). | RustFS refuses a session policy over 2,048 bytes, measured after it re-serialises the parsed policy: `rustfs@3b0ee83:rustfs/src/admin/service/session_policy.rs:34-39`. It re-serialises without empty fields (`crates/policy/src/policy/policy.rs:57-66`; `crates/policy/src/policy/statement.rs:26-41`) and without the spaces Python's `json.dumps` adds, so the counted size should not exceed MinIO's. | **Met in source** (same limit). Unclear at the boundary: not measured. |
| R6 | `DurationSeconds=900` is honoured. | `chart/values.yaml:842`; `vending.py:734`. | AssumeRole clamps to 900–43200 s: `rustfs@3b0ee83:rustfs/src/admin/handlers/sts.rs:64-83`. | **Met in source.** |
| R7 | The `RoleArn` rask sends is a placeholder that the store ignores. | `chart/values.yaml:850`; `services/catalog/src/catalog/core/config.py:104`. | AssumeRole does not put `RoleArn` in the session claims: `sts.rs:249-283`. The STS evaluator uses a role mapping only when a `roleArn` claim exists: `crates/iam/src/sys.rs:1548,1555-1568`; `crates/policy/src/policy/policy.rs:49-51`. AssumeRoleWithWebIdentity reads neither `RoleArn` nor `RoleSessionName`: neither appears in `rustfs/src/admin/service/federated_identity.rs`. The docs say RoleArn is "accepted for AWS compatibility". | **Met in source.** |
| R8 | Key rotation (ADR 0166): a pair that signs AssumeRole must rotate to another USER, not an access key. | No rask pair signs AssumeRole (ADR 0170): the catalog holds no pair, and lineage's and maintenance's sign S3 requests only, so they rotate as ADR 0166 states. | RustFS also refuses AssumeRole from a service account (access key): `rustfs@3b0ee83:rustfs/src/admin/handlers/sts.rs:219-221`. The docs page says the same. | **Same as MinIO.** ADR 0166's "second user under the same policy" model carries over unchanged. |
| R9 | Put-if-not-exists (`If-None-Match: *`) answers 412 to the loser, and concurrent conditional writers are arbitrated atomically. This is Lance's commit primitive and rask's registry CAS. | Lance needs put-if-not-exists or rename-if-not-exists so that "exactly one writer succeeds": `lance_docs/file_format.md:6709-6716`. rask's registry create: `packages/service-kit/src/service_kit/lakehouse/records.py:95-111`. The warehouse probe checks that a second conditional put is refused: `services/catalog/src/catalog/api/v1/endpoints/warehouses.py:1282-1291`. Contended harness: `tests/e2e-py/test_object_store_cas_e2e.py:1-19`. | The header is parsed for PutObject and CompleteMultipartUpload: `rustfs@3b0ee83:rustfs/src/storage/options.rs:316-329,421,471`. PutObject does an advisory check before the body: `crates/ecstore/src/set_disk/ops/object.rs:4214-4226`. It takes the object's namespace write lock at commit: `object.rs:4771`. It re-checks under that lock: `object.rs:4780-4784`. An existing object fails the check: `crates/ecstore/src/set_disk/core/io_primitives.rs:6405-6442`. That error maps to S3 `PreconditionFailed`: `rustfs/src/error.rs:807`. | **Met in source** (check under lock). Live: pending. |
| R10 | `If-Match: <etag>` on PutObject, for the registry's read-modify-write. | `records.py:235-256`. | Same path as R9, `should_prevent_write` with `if_match`: `io_primitives.rs:6427-6433`. | **Met in source.** |
| R11 | Conditional CompleteMultipartUpload (a large manifest or record may be multipart). | Lance's commit protocol (R9). rask itself sends conditional headers only on single `put_object` (`records.py:105,249`). | The app layer checks early: `rustfs@3b0ee83:rustfs/src/app/multipart_usecase.rs:585-627`. The store takes the object write lock, then checks: `crates/ecstore/src/set_disk/ops/multipart.rs:2702-2713`. It holds the lock through commit: `multipart.rs:2733-2742`. | **Met in source.** |
| R12 | Multipart uploads from pyarrow 25's `S3FileSystem` (the record store) and from Lance's object_store. | Record writes go through `open_output_stream`: `packages/service-kit/src/service_kit/lakehouse/record_store.py:54-61`. The filesystem: `packages/service-kit/src/service_kit/lakehouse/objectfs.py:240-267`. rask measured alpha.60 refusing pyarrow 25's `UploadPart` with `XAmzContentSHA256Mismatch`, and 1.0.1 accepting it: `.dagger/storage.go:9-15`; `docs/agents/lessons.md:97-101`. | `STREAMING-UNSIGNED-PAYLOAD-TRAILER` is a named constant: `rustfs@3b0ee83:rustfs/src/auth.rs:103`. Streaming size handling is tested: `rustfs/src/app/object/put.rs:3800-3815`. The commit that fixed the mismatch was not found (see Could not confirm). | **Met at 1.0.1** (rask's own measurement). Fix commit: not identified. |
| R13 | Bucket create and delete by the catalog for a warehouse with its own bucket, including a paginated purge with `DeleteObjects`. | `services/catalog/src/catalog/services/warehouses.py:64-87` (create), `:605-630` (purge). The catalog policy grants `s3:CreateBucket` and `s3:DeleteBucket`: `chart/templates/minio-scoped-users.yaml:196-201`. | Not traced in RustFS source. rask's `rustfs-lifecycle` lane runs the catalog lifecycle on RustFS 1.0.1: `.dagger/storage.go:63-82,15`. | **Unclear.** Likely met. The purge path was not read on either side. |
| R14 | Server-side encryption options carried in vended storage options. | `objectfs.py:148-178`; `vending.py:91-111`. Empty by default, aimed at an AWS deployment: `chart/values.yaml:864-875`. | RustFS implements SSE-S3, SSE-KMS and SSE-C. SSE-KMS without a configured KMS returns 400 or 503: `rustfs@3b0ee83:CHANGELOG.md:30-44` (Unreleased § Fixed). | **Not required for the fixture** (default empty). Met if a KMS is configured. Not verified. |
| R15 | Session token forwarded by every client that uses a vended credential. | Lance options: `objectfs.py:99-100`. pyarrow: `objectfs.py:252-264`. boto3 records: `records.py:63`. | Store-agnostic. RustFS issues a session token: `sts.rs:323-333`. | **Met** (client side). |
| R16 | Scoped users and policies provisioned by `mc admin` (`policy create`, `user add`, `policy attach`). | `chart/templates/minio-scoped-users.yaml:86,149-151,182-184,206-208,226-228,248-252`. `mc` is pinned to `RELEASE.2025-08-13T08-35-41Z`: `.docker/minio.dockerfile:24-25`. | RustFS serves `/minio/admin/...` as an alias of `/rustfs/admin/...`: `rustfs@3b0ee83:rustfs/src/server/prefix.rs:54-68`; `rustfs/src/admin/router.rs:3154-3162`. A body on the `/minio/admin` path is decrypted the way madmin encrypts it: `rustfs/src/admin/utils.rs:31-62`. The routes exist: add-user `rustfs/src/admin/handlers/user_lifecycle.rs:42-46`; policy attach `rustfs/src/admin/handlers/policies.rs:93`; add-canned-policy and set-policy are in the route test `rustfs/src/admin/route_registration_test.rs:163-166`. | **Met in source.** Whether this `mc` release interoperates: pending live. |
| R17 | No runtime admin calls from a service. | No `mc admin`, `/minio/admin` or `madmin` in `services/`, `packages/` or `scripts/` Python (grep, 2026-10-18). The only store-specific runtime path is the fixture's health probe: `deploy/fixtures/minio/chart/templates/minio.yaml:82,86`; `tests/e2e-py/chaos.py:284`. | n/a | **Met.** A swap touches the fixture chart and the hook job, not service code. |
| R18 | AssumeRoleWithWebIdentity for the cluster's ServiceAccount issuer, granting a token the policy its subject names: the catalog's own IO and every vend, so the catalog holds no store credential at all (ADR 0170). | `services/catalog/src/catalog/core/vending.py` (`WebIdentityVendor`); `packages/service-kit/src/service_kit/governed/store_session.py`; `chart/values.yaml` `objectStore.identity`, `vending.mode: web_identity`; `deploy/fixtures/rustfs/chart/values.yaml` `identityProvider`. | Supported since 1.0.0 (alpha.60 refused it): `rustfs@3b0ee83:rustfs/src/admin/handlers/sts.rs:186-195,399-441`. The token's signature, issuer, audience and expiry are checked: `rustfs@1.0.3:crates/iam/src/oidc/runtime.rs:740-798`. With `claim_name=sub` and no `role_policy`, a token gets the canned policy its `sub` names: `rustfs@1.0.3:crates/iam/src/federation/mapper.rs:257-298`. | **Measured** on 1.0.3 with a Kubernetes-shaped token and a stand-in issuer, 20 of 20 checks (§ "Measured: web identity"). Read back live 2026-10-18 at helm rev 3 (`XC-083 3e3924df`, rustfs fixture rev 2): the store verifies the cluster's own ServiceAccount tokens through rask's mirror; the catalog's session is issued from its token and every vend after it; a token for another account (`rask-sa-jobs`, audience `rask-object-store`) is refused 400 `InvalidRequest`, "OIDC policy mapping did not resolve to current policies". |
| R19 | Non-root image, anonymously pullable, multi-arch. | The fixture runs MinIO from a source build because `minio/minio` and `minio/mc` refuse anonymous pulls: `.docker/minio.dockerfile:4-8`. rask's Dagger lanes already pull `rustfs/rustfs:1.0.1`: `.dagger/storage.go:15`. rask measured uid 10001: `.dagger/storage.go:17-20`. | The image creates and runs as uid/gid 10001: `rustfs@3b0ee83:Dockerfile:107-125`. CI pushes to `rustfs/rustfs` and `quay.io/rustfs/rustfs`: `.github/workflows/docker.yml:67-69,468`. Docker Hub tag `1.0.3` lists amd64 and arm64 (hub.docker.com API, 2026-10-18). | **Met.** |
| R20 | A maintained upstream that ships security fixes. | CLAUDE.md "three facts": zero trust, cloud native, Lance-idiomatic. | Apache-2.0 (GitHub license API). GA releases: 1.0.0 2026-09-16, 1.0.1 2026-10-03, 1.0.2 2026-10-16, 1.0.3 2026-10-17 (GitHub releases API). Recent fixes include a SigV4 unsigned-header bypass (GHSA-xm99-m3gq-83g8) and presigned URLs (GHSA-g8w9-qw9q-fghr): `CHANGELOG.md:10-12,24-25`. | **Met.** |

### A difference that matters for vended credentials

- MinIO advisory GHSA-jjjj-jwhf-8rgr (high) says this: an STS or service account with a restricted session policy
  could create a service account for itself, and that new account got the parent's full privileges. MinIO's
  `DenyOnly` own-account check skipped the session policy. Affected: "all". Patched in `RELEASE.2025-10-15T17-29-55Z`
  (GitHub security advisory API).
- rask hands session-scoped credentials to any authenticated client: `docs/adr/0144-storage-credentials-are-vended-to-any-authenticated-client.md:22-26`.
  The parent of those credentials is the catalog's ServiceAccount, whose policy holds the whole data plane
  (`chart/templates/object-store-scoped-users.yaml`, ADR 0170). The advisory's scenario matches that shape. Whether the catalog
  user may create a service account on MinIO by default was not measured.
- The fixture runs MinIO `RELEASE.2025-04-22T22-12-26Z`: `.docker/minio.dockerfile:19-23`. That is before the fix.
- RustFS's `add-service-account` requires an explicit Allow for `admin:CreateServiceAccount`, never deny-only:
  `rustfs@3b0ee83:rustfs/src/admin/handlers/service_account.rs:362-390`. For a temporary credential, that check goes
  through the STS path, which also requires the session policy to allow it: `crates/iam/src/sys.rs:1450-1458`.
  rask's session policies grant S3 actions only (`vending.py:142-150,576-663`).

## MinIO's status (primary sources)

- The repository is archived. The README opens with "THIS REPOSITORY IS NO LONGER MAINTAINED" and points to AIStor
  Free and AIStor Enterprise (github.com/minio/minio README; GitHub API `archived=true`).
- The community edition is "distributed as source code only", with no more pre-compiled binary releases (README §
  "Source-Only Distribution").
- The license is AGPL-3.0 (GitHub license API; README).
- The last community release is `RELEASE.2025-10-15T17-29-55Z` (GitHub releases API). The last commits are README
  edits: "update README.md maintenance mode" on 2025-12-03 and "clarify state of the project" on 2026-02-12 (GitHub
  commits API).
- `minio/mc` is archived too (GitHub API `archived=true`).
- Advisories published after rask's pin (GitHub security advisory API):
  - GHSA-jjjj-jwhf-8rgr: STS/service-account session-policy bypass, patched 2025-10-15. That fix exists in community
    source, newer than rask's pin.
  - GHSA-hv4r-mvr4-25vw (high): unauthenticated object write through a query-string credential on
    `STREAMING-UNSIGNED-PAYLOAD-TRAILER` uploads. Affected from `RELEASE.2023-05-18`. Patched `>= RELEASE.2026-04-11T03-20-12Z`.
  - GHSA-9c4q-hq6p-c237 (high): unauthenticated object write through Snowball auto-extract. Same affected range and patch.
  - No tag `RELEASE.2026-04-11T03-20-12Z` exists in `minio/minio` (GitHub refs API answers 404). So no community
    source carries the 2026 fixes, and a source-built fixture cannot pick them up.
- What rask pays to keep MinIO:
  - A two-stage Go build of `minio` and `mc` from pinned tags and commits: `.docker/minio.dockerfile:1-80`. It exists
    because the upstream images refuse anonymous pulls (XC-075): `.docker/minio.dockerfile:4-8`.
  - The fixture chart `deploy/fixtures/minio/` and the hook job `chart/templates/minio-scoped-users.yaml`.
  - The kind lane installs that fixture: `scripts/e2e_stack.sh:9,229`.

## Measured on RustFS (main session)

Measured 2026-10-18 on `rustfs/rustfs:1.0.3@sha256:9ca9559533b537d10482fe923288b676ad08f92a7069d9a17327fbae3cab688b`
(the pin) and on `rustfs/rustfs:1.0.1@sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c`, each a
fresh single node run through Dagger with only its root pair set (`RUSTFS_ACCESS_KEY`/`RUSTFS_SECRET_KEY`), driven from
the host by `rustfs_sts_measure.py` (beside this file) with boto3, pylance 13.0.0 and pyarrow 25.0.0. Users and
policies were made through RustFS's own admin API (`/rustfs/admin/v3/add-canned-policy`, `/add-user`,
`/set-user-or-group-policy`, plain JSON bodies, SigV4 with the root pair), the shape of rask's scoped-users hook: a
canned policy granting `s3:*` on two tenant buckets and naming no `sts:` action, attached to a user `catalog`. 26
checks, all as rask needs them, on both releases:

| Check | Answer |
|---|---|
| The scoped user reads its own bucket; a bucket its policy does not name is refused | 200; `AccessDenied` (403) |
| **The scoped (non-root) user's `AssumeRole` with a session policy issues credentials** (R1) | issued, `DurationSeconds=900` honoured (R6) |
| Read session on `tenant-a/t1/*`: GET in scope | 200 |
| Same session: GET `tenant-a/t2/…` (same bucket, other prefix), GET `tenant-b/…` (other tenant) | `AccessDenied` (403) both (R2) |
| Same session: PUT and DELETE in scope | `AccessDenied` (403) both: a read session cannot write |
| Same session: LIST prefix `t1/` allowed, LIST prefix `t2/` refused | 200; `AccessDenied` (403) (R4) |
| A session policy naming a bucket the parent does not hold grants nothing there | `AccessDenied` (403): the session cannot widen the parent |
| Write session on `tenant-a/t1/*`: PUT in scope; PUT another prefix; PUT another tenant | 200; `AccessDenied`; `AccessDenied` |
| **Lance creates and appends a dataset at `s3://tenant-a/t1/ds.lance` with the vended session credential** | 6 rows, 2 commits (R15) |
| Lance writing outside the vended prefix | refused (`AccessDenied` on its listing) |
| pyarrow's record-store write (`put_record`, rask's path, with the catalog user's own pair) | written and read back (R12) |
| pyarrow's write through the prefix-scoped write session | refused at `HeadBucket` (`ACCESS_DENIED`), as rask needs |
| Root's `AssumeRole` (baseline) | issued |
| A user whose policy carries an explicit `Deny` on `sts:AssumeRole` | `AccessDenied`: deny-only evaluation as documented |
| `PUT If-None-Match: *` twice on one key | 200, then `PreconditionFailed` (412) (R9) |
| `PUT If-Match` with the current ETag, then with the stale one | 200, then `PreconditionFailed` (412) (R10) |
| 16 writers racing `If-None-Match: *` on a fresh key, 20 rounds | exactly one winner in every round (R9) |

pyarrow asks `HeadBucket` before it writes, and RustFS authorizes that as an unprefixed `s3:ListBucket` on the bucket
(`rustfs@1.0.3:rustfs/src/storage/access.rs:2955-2960`), so a session that may list only under an `s3:prefix`
condition is refused it, as AWS refuses it. rask's record store writes with a service's own scoped pair, never with a
vended credential; a vended credential writes through Lance's object_store, which never asks.

Every RustFS source range this audit and `.claude/skills/rask-rustfs` cite was compared, line for line, between
`3b0ee83` and the 1.0.3 tag (`331fb896285a7686f927baf8d9d4dc2d3d3a10a9`): 222 read the same, 20 read the same at shifted
lines, and 5 differ. Three are version strings (`Cargo.toml:77`, and `helm/rustfs/Chart.yaml:5-6` cited twice: 1.0.1 in
the 1.0.3 tree). One is `startup_fs_guard.rs:56-68`, whose filesystem check reads the same at 55-61. The fifth is R11's
app-layer early check (`multipart_usecase.rs:585-627`): at 1.0.3 it refuses `If-None-Match` only for a named ETag, not
for `*`, so the store-level check under the object's write lock decides alone, and that range
(`crates/ecstore/src/set_disk/ops/multipart.rs:2702-2742`) reads the same at 1.0.3. The skill cites the 1.0.3 tag.

Not measured here: `mc admin` against RustFS (R16: rask's `mc` build could not be pulled from the local registry
through this session's Dagger engine), conditional CompleteMultipartUpload (R11), the warehouse purge (R13), a
multi-node deployment, and the 2,048-byte boundary (R5). AssumeRoleWithWebIdentity (R18) is measured in its own section
below.

## Measured: web identity

Measured 2026-10-18 on `rustfs/rustfs:1.0.3@sha256:9ca9559533b537d10482fe923288b676ad08f92a7069d9a17327fbae3cab688b`,
a fresh single node run through Dagger with its root pair set, configured as the rustfs fixture configures it
(`deploy/fixtures/rustfs/chart/values.yaml` `identityProvider`): `RUSTFS_IDENTITY_OPENID_CONFIG_URL` at a stand-in issuer
on the host, `RUSTFS_IDENTITY_OPENID_ISSUER=https://kubernetes.default.svc.cluster.local`,
`RUSTFS_IDENTITY_OPENID_CLIENT_ID=rask-object-store`, `RUSTFS_IDENTITY_OPENID_CLAIM_NAME=sub`,
`RUSTFS_IDENTITY_OPENID_HIDE_FROM_UI=true`, `RUSTFS_OUTBOUND_ALLOW_ORIGINS` the issuer's origin, and no role policy. The
stand-in issuer (`rustfs_web_identity_measure.py serve`, beside this file) serves what rask's issuer mirror serves: a
discovery document whose `issuer` is the cluster's and whose `jwks_uri` points at itself, with no
`authorization_endpoint`, and its key set. Tokens carry the claims k3s projects (`iss`, `sub`, `aud`, `exp`, `iat`, `nbf`,
`jti`, `kubernetes.io`), RS256 with a `kid`. 20 checks, all as rask needs them:

| Check | Answer |
|---|---|
| A canned policy named by the catalog's ServiceAccount subject (`system:serviceaccount:default:rask-sa-catalog`, colons) is created | 200 |
| The catalog's token, no session policy | issued: the policy its subject names |
| That session writes where the canned policy allows; reads a bucket it does not name | 200; `AccessDenied` (403) |
| `DurationSeconds=3600` | honoured (expires in 3,599 s) |
| A read session on `tenant-a/t1/*`: GET in scope; another prefix; another tenant; a PUT in scope | 200; 403; 403; 403 |
| A session policy naming a bucket the canned policy lacks | grants nothing (403) |
| Lance creates and appends a dataset through a write session | 6 rows, 2 commits |
| Another ServiceAccount's token for the same audience; a same-named account in another namespace | refused: `InvalidRequest` (400), "OIDC policy mapping did not resolve to current policies" |
| The catalog's token for another audience; an expired token; one signed by another key; one from another issuer | refused: `AccessDenied` (403), "token verification failed" (invalid audience, expired, signature, no provider for the issuer) |
| `GET /rustfs/admin/v3/list-users` | plain JSON keyed by access key; names no STS session |
| `DELETE /rustfs/admin/v3/remove-user` | 200, then 404 for the same user |

The issuer's log shows the store fetched discovery and the key set at boot, and fetched both again for each token that
failed verification: RustFS retries a failed verification once after refreshing the provider
(`rustfs@1.0.3:crates/iam/src/oidc/runtime.rs:740-798`), so every forged or expired token costs one round trip to the
issuer. A request to an admin route with a bare `?` and no parameters is refused 400 "failed to decode query"
(`scripts/rustfs_admin.py` sends none).

## What the sources prove and do not prove

Proven by source (RustFS `3b0ee83`, gate also present at tags 1.0.0, 1.0.1 and 1.0.3):

- A regular IAM user with at least one resolving policy may call AssumeRole, unless an explicit Deny names
  `sts:AssumeRole` (R1).
- The session policy is ANDed with the parent's policy on every request, and its Deny wins (R2, R3).
- Conditional PutObject and CompleteMultipartUpload re-check their precondition under the object's namespace write
  lock (R9–R11).
- `/minio/admin` is an alias with madmin-compatible body encryption (R16).
- The image runs as uid 10001 and is multi-arch (R19).

Not proven:

- That any of this works at runtime. Nothing here was run.
- That the namespace lock is exclusive across nodes in a distributed deployment. The fixture is single-node, and only
  the single-set path was read.
- That `mc RELEASE.2025-08-13` drives RustFS's admin API without error.
- What produced the 403 rask measured on 2026-09-11, and on which RustFS version. The alpha.60 handler does not
  explain it.
- That rask's warehouse purge (`DeleteObjects` then `DeleteBucket`) works on RustFS. It was not traced.

Contradictions inside rask's own record:

- None remains in `vending.py`: its module docstring states what the measurements in this file show, and its one
  credential-issuing plug exchanges the catalog's own token (ADR 0170).

## Does rask need MinIO at all?

- **In production, no.** S3 is a platform service, and the bundled store is a development fixture (CLAUDE.md,
  "Platform services are external in production"). rask must name the features it demands of any platform store
  (below), not a product.
- **For the development fixture and the kind lane:** MinIO's only advantage on record was that a scoped user could
  assume a role (ADR 0063). RustFS 1.0.0+ source removes that gap (R1, R2). MinIO's community source is archived, and
  rask's pin predates a published STS session-policy bypass fix. So keeping MinIO now costs zero-trust standing, and
  does not buy it. Whether to switch waits on the main session's live measurement.
- **The Dagger lanes already run on RustFS 1.0.1:** the storage smoke, `rustfs-lifecycle` and the governed lanes
  (`.dagger/storage.go:15`; `.dagger/governed.go:83-102`). They use the root key and the catalog's default
  `vending_mode="mode_b"` (`services/catalog/src/catalog/core/config.py:386`). So today STS vending is exercised on
  MinIO only, through the kind lane.

### What rask must demand of ANY platform store (requirement list)

1. STS `AssumeRoleWithWebIdentity` that trusts the cluster's ServiceAccount issuer for the catalog's audience and grants
   a token only the policy its subject is given (a canned policy named by `sub`, or a role whose trust policy names
   it), with an inline session policy honoured (R18, ADR 0170). No rask identity signs `AssumeRole` with a key.
2. An inline session policy, enforced as an intersection with the parent's policy on every request. An explicit Deny
   must win. The size budget must be at least 2,048 bytes (R2, R3, R5).
3. Resource ARNs with key-prefix wildcards, and an `s3:prefix` `StringLike` condition on ListBucket (R4).
4. `DurationSeconds` of 900 s accepted (R6).
5. `If-None-Match: *` and `If-Match` on PutObject (and on CompleteMultipartUpload), answering 412, with exactly one
   winner under contention (R9–R11; `lance_docs/file_format.md:6709-6716`).
6. SigV4 multipart uploads, including `STREAMING-UNSIGNED-PAYLOAD-TRAILER` as pyarrow 25 sends it (R12).
7. Bucket create, delete and multi-object delete for a warehouse that owns its bucket (R13).
8. The session token honoured on every data-plane call (R15).
9. Optional: SSE (AES256 or aws:kms) when the deployment sets `vending.encryption` (R14).
10. A maintained upstream that ships security fixes (R20).

## Open questions

- Answered: a Kubernetes projected ServiceAccount token acts as the web identity on RustFS 1.0.3. The cluster's
  issuer is a workload provider: its discovery has no `authorization_endpoint`, which RustFS accepts under
  `HIDE_FROM_UI` (`rustfs@1.0.3:crates/iam/src/oidc/provider.rs:128-153`). k3s serves discovery only to a bearer, so the
  store reads it from rask's issuer mirror: with an explicit `ISSUER`, RustFS fetches discovery from `CONFIG_URL` and
  requires the discovered `issuer` to equal the explicit one (`provider.rs:498-588`), the mirror's private origin being
  the one `RUSTFS_OUTBOUND_ALLOW_ORIGINS` admits (`rustfs@1.0.3:crates/utils/src/egress.rs:25-29`). A token needs no
  `groups` claim: with `CLAIM_NAME=sub` and no role policy it is granted the canned policy its `sub` names
  (`rustfs@1.0.3:crates/iam/src/federation/mapper.rs:257-298`), and a `sub` naming none is refused 400
  (`rustfs@1.0.3:rustfs/src/admin/service/federated_identity.rs:322-440`). Measured in § "Measured: web identity".
- Which RustFS commit fixed pyarrow 25's `UploadPart` `XAmzContentSHA256Mismatch` between alpha.60 and 1.0.1?
- Does the namespace write lock serialise conditional writes across nodes, the way the single-node path does?
- Answered: `vending.py`'s prose is rewritten to the measurements (ADR 0170).
