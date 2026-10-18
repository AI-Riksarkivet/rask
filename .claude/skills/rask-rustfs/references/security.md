# RustFS security for rask

Citations: `rustfs@1.0.3:<path>:<line>`, `docs:<path>`, rask paths bare. Judge every option against the four facts,
GitOps first: GitOps, cloud native, Lance-namespace/table-idiomatic, zero trust (CLAUDE.md § Engineering principles).

## Root credential

- Sources, in precedence order: inline flag/env, then the file (`RUSTFS_ACCESS_KEY_FILE`/`RUSTFS_SECRET_KEY_FILE`), then
  env, then the legacy `RUSTFS_ROOT_USER`/`RUSTFS_ROOT_PASSWORD`, then the default `rustfsadmin`
  (`rustfs@1.0.3:rustfs/src/config/config_struct.rs:54-78,216-231`; `rustfs@1.0.3:rustfs/src/config/cli.rs:1551-1564`).
  The binary reads the whole file and trims it (`config_struct.rs:64`); the image's `entrypoint.sh` reads the first
  line only and exits 1 on an empty or unreadable file (`rustfs@1.0.3:entrypoint.sh:156-175`).
- No root pair → `rustfsadmin`/`rustfsadmin` and a warning; the server starts
  (`rustfs@1.0.3:crates/credentials/src/constants.rs:21,28`; `rustfs@1.0.3:rustfs/src/startup_server.rs:100-109`).
- The root secret signs every STS session token (GHSA-m77q-r63m-pj89, intentionally unfixed,
  `rustfs@1.0.3:crates/iam/src/root_credentials.rs:25-36`). Consequences: whoever reads the root secret can forge
  any session; rotating root invalidates every outstanding vended credential (at most one 900 s TTL of failed reads
  at rask's TTL).
- No online root rotation API: change the secret and restart (`docs:operations/credentials.md`, which labels the
  procedure as composed practice). Whether a changed `_FILE` is re-read at runtime is not checked.
- Internode RPC uses `RUSTFS_RPC_SECRET`, or an HMAC derived from the root pair; derivation fails closed if EITHER half
  is the default (`rustfs@1.0.3:crates/credentials/src/credentials.rs:256-273`). The docs say only "an all-default
  pair" (contradiction, detail: `docs:reference/environment-variables.md`). Single node: not needed. Multi-node: set it
  explicitly so a root rotation does not break the cluster.

## IAM and policies

- Statement grammar: Sid, Effect, Action/NotAction, Resource/NotResource, Condition; unknown fields refused
  (`rustfs@1.0.3:crates/policy/src/policy/statement.rs:25-42`). Evaluation: a matching Deny first, then deny-only
  short-circuit, then owner, then a matching Allow, else deny (`rustfs@1.0.3:crates/policy/src/policy/policy.rs:57-96`).
- Condition keys rask uses exist: `s3:prefix` with `StringLike`
  (`rustfs@1.0.3:crates/policy/src/policy/function/key_name.rs:219-220`;
  `rustfs@1.0.3:crates/policy/src/policy/function/condition.rs:32,69`). `aws:SecureTransport` exists; no ABAC tag
  keys (`aws:PrincipalTag/*`, `aws:RequestTag/*`, `aws:ResourceTag`) exist (`condition.rs:26-100`; `key_name.rs`).
- Policy variables resolve only `aws:username`, `aws:userid` and a few others; `${jwt:sub}` is NOT substituted
  (`rustfs@1.0.3:crates/policy/src/policy/variables.rs:215-232`). Per-table scoping comes from the session policy the
  catalog builds, never from a variable.
- Canned policies: eight exist (readwrite, readonly, writeonly, diagnostics, consoleAdmin, KMSKeyAdministrator,
  KMSKeyUser, KMSAuditor), each also allowing `sts:AssumeRole` (`policy.rs:370-410`). The docs list five (contradiction:
  `docs:security-compliance/iam/policies.md`). rask attaches none of them; do not name a custom policy after one.
- `s3:*` does not grant `s3:ForceDeleteBucket`/`s3:ForceDeleteObject` (`rustfs@1.0.3:crates/policy/src/policy/action.rs:184-198`);
  the force header then also needs an authenticated caller (`rustfs@1.0.3:rustfs/src/storage/access.rs:117-126,2401-2416`).
- Service accounts need an explicit `admin:CreateServiceAccount` Allow, even for one's own account
  (`rustfs@1.0.3:rustfs/src/admin/handlers/service_account.rs:362-390`). The docs say self-creation needs no grant
  (contradiction: `docs:security-compliance/iam/sts.md`).
- Bucket policies: none in the fixture; all access goes through IAM users and STS. With BlockPublicPolicy on, an Allow
  with `Principal: "*"` is refused (`rustfs@1.0.3:rustfs/src/app/bucket_usecase.rs:2659-2670`); `IsPublic` judges only
  anonymous ListBucket and PutObject, so it is no proof that a bucket is private (`bucket_usecase.rs:2172-2224`).
- An external OPA authorizer exists (`RUSTFS_POLICY_PLUGIN_URL`, `rustfs@1.0.3:crates/config/src/opa/mod.rs:16`);
  rask leaves it unset: authorization is OpenFGA at the catalog, and the store enforces the session policy.

## STS AssumeRole

- Served at the root: `POST /`, form-encoded, `Version=2011-06-15`; only `AssumeRole` and `AssumeRoleWithWebIdentity`
  (anything else: `InvalidArgument "unsupported Action"`, `rustfs@1.0.3:rustfs/src/admin/handlers/sts.rs:60-62,187-193`).
  No `GetSessionToken`.
- Refuses a request carrying a session token, a temporary caller and a service account (`sts.rs:212-221`); evaluates
  `sts:AssumeRole` deny-only (`sts.rs:225-240`); requires a resolving policy binding
  (`rustfs@1.0.3:crates/iam/src/sys.rs:1494-1525`); MFA-enrolled identities need a TokenCode (`sts.rs:250-270`).
- Duration clamped to 900–43,200 s, default 3,600 (`sts.rs:64-83`). `RoleArn`, `RoleSessionName`, `ExternalId` are
  accepted and ignored (audit R7).
- Every vend persists a temp user in the IAM store (`sts.rs:311-314`). An expired identity is deleted, with its policy
  mapping, when the store loads it (`rustfs@1.0.3:crates/iam/src/store/object.rs:628-633`), and a full load walks every
  user (`object.rs:904-930`). A full load runs every 120 s unless `RUSTFS_SKIP_BACKGROUND_TASK` is set
  (`rustfs@1.0.3:crates/iam/src/manager.rs:209-224`), so the store holds about one identity for each vend of the last
  900 + 120 s: bounded by the vend rate, since rask vends per request and caches nothing. A vend cache (Lakekeeper keeps one,
  single-flight per table and permission) would shrink that and hand one credential to several callers, which the
  store's audit cannot tell apart; rask does not cache.

## Web identity (R18)

- `AssumeRoleWithWebIdentity`: `WebIdentityToken`, no SigV4, same clamp, optional inline session policy
  (`sts.rs:399-441`; `rustfs@1.0.3:rustfs/src/admin/service/federated_identity.rs:242-300`). The session's parent is a
  virtual user derived from `sub` and `iss` (`rustfs@1.0.3:crates/iam/src/federation/mapper.rs:112-129`).
- Provider config is env only, `RUSTFS_IDENTITY_OPENID_*` (`rustfs@1.0.3:crates/config/src/constants/oidc.rs:35-52`);
  a suffix `_<ID>` defines another provider, and a token is routed to the provider whose issuer matches its `iss`
  (`rustfs@1.0.3:crates/iam/src/oidc/config.rs:566-602`). The env reference page omits these variables, and the OIDC
  page describes only Console SSO (gap: `docs:security-compliance/oidc/index.md`).
- Audience: the verifier's client id is `RUSTFS_IDENTITY_OPENID_CLIENT_ID`; extra audiences go in `..._OTHER_AUDIENCES`.
  Nonce is skipped for web identity; JWKS is refreshed once on failure
  (`rustfs@1.0.3:crates/iam/src/oidc/runtime.rs:74-81,731-797`).
- Claim mapping: with `..._ROLE_POLICY` set, every verified token gets exactly those policies and no claim selects
  them; otherwise policies are the groups-claim values (with the `..._ROLES_CLAIM` values when that is set,
  `runtime.rs:835-847`), plus the `..._CLAIM_NAME` claim's values when it names a
  different claim, each with `..._CLAIM_PREFIX` prepended (`mapper.rs:257-298`; `runtime.rs:827-845`). A token mapping
  to no policy and no group is refused (`rustfs@1.0.3:crates/iam/src/federation/transaction.rs:135-148`).
- **Hazard**: a role policy is granted to ANY token the issuer signs for that audience, and the token holder can call
  STS without a session policy. In rask that means: a role policy must be no broader than what any holder of such a
  token may do, with a dedicated audience/client. A Dex user token for the catalog's client must not be exchangeable
  for the catalog's full data plane.
- A Kubernetes ServiceAccount issuer works as a "workload" provider when `..._HIDE_FROM_UI` is on and its discovery has
  no `authorization_endpoint` (`rustfs@1.0.3:crates/iam/src/oidc/provider.rs:104-153,375-386`). This is the mode where
  the catalog holds no store key at all: a projected token with audience = client id, a minimal role policy, and the
  catalog's per-table session policy. Source only; not measured.
- Outbound OIDC discovery/JWKS goes through an egress guard that refuses loopback, private, shared and reserved
  addresses unless the exact origin is listed in `RUSTFS_OUTBOUND_ALLOW_ORIGINS`; link-local and metadata addresses stay
  forbidden (`rustfs@1.0.3:crates/utils/src/egress.rs:25-29,362-372,490-530`). In-cluster Dex and the API server are
  private addresses: list their origins (and every private origin in their discovery's `jwks_uri`).
- The OIDC client secret has no `_FILE` variant (grep of `crates/config`): prefer a public client for STS-only use.

## Provisioning (the hook)

Native admin API under `/rustfs/admin/v3`, SigV4 (service `s3`) with the root pair, plain JSON bodies; measured
2026-10-18 for add-canned-policy, add-user and set-user-or-group-policy (audit § Measured):

- `PUT add-canned-policy?name=` (`rustfs@1.0.3:rustfs/src/admin/handlers/policies.rs:71`)
- `PUT add-user?accessKey=`, body `{"secretKey":"…","status":"enabled"}` (`rustfs@1.0.3:rustfs/src/admin/handlers/user_lifecycle.rs:44`)
- `PUT set-user-or-group-policy?policyName=&userOrGroup=&isGroup=false`, replaces the binding (`policies.rs:83`)
- `POST idp/builtin/policy/attach`, adds to it (`policies.rs:93`)
- `DELETE remove-user`, `DELETE remove-canned-policy` (`user_lifecycle.rs:38`; `policies.rs:77`)
- `GET export-iam` (`rustfs@1.0.3:rustfs/src/admin/route_registration_test.rs:159`)

The same routes answer under `/minio/admin` with madmin's body encryption
(`rustfs@1.0.3:rustfs/src/admin/utils.rs:31-62`), which is what `mc admin` would hit; that is unmeasured, and `mc` is
archived upstream. A Python script with botocore's SigV4 signer and boto3 for `CreateBucket` needs no new tool. Admin
request bodies are capped at 1 MiB (`rustfs@1.0.3:crates/config/src/constants/body_limits.rs:24`).

## TLS

- `RUSTFS_TLS_PATH` names a directory with `rustfs_cert.pem` and `rustfs_key.pem`; empty means plain HTTP
  (`rustfs@1.0.3:crates/config/src/constants/app.rs:220,264`; `rustfs@1.0.3:rustfs/src/config/cli.rs:1593-1594`).
  Hot reload is off by default (`RUSTFS_TLS_RELOAD_ENABLE`, 30 s interval,
  `rustfs@1.0.3:crates/config/src/constants/tls.rs:198-209`): turn it on with cert-manager rotation.
- Server mTLS (`RUSTFS_SERVER_MTLS_ENABLE`, default false, `tls.rs:86`) requires a client certificate from EVERY S3
  client; vended credentials carry none, so plain server TLS is rask's option.
- The Java SDK page says RustFS "only supports HTTP"; the source serves TLS on the API and console (contradiction:
  `docs:developer/sdk/java.md` vs `cli.rs:1593-1594`).
- Forwarded headers are trusted only from `RUSTFS_TRUSTED_PROXY_NETWORKS`, default loopback
  (`rustfs@1.0.3:crates/config/src/constants/proxy.rs:19-69`). If a Gateway ever fronts the store, the route must be
  host-based with no `URLRewrite` (SigV4 signs host and path).

## KMS and SSE

- Off by default (`RUSTFS_KMS_ENABLE=false`); backends local, vault/vault-kv2, vault-transit, static, aws
  (`rustfs@1.0.3:rustfs/src/config/cli.rs:1603-1638`; `rustfs@1.0.3:crates/kms/src/config.rs:1056-1060`). The docs
  omit static and aws.
- Vault auth: static token, token file, AppRole or Kubernetes auth (`crates/kms/src/config.rs:25-65,1347-1405`). The
  docs imply a static token only (gap). Kubernetes auth gives SSE-KMS with no static secret. OpenBao compatibility is
  inferred from API compatibility, not stated or measured.
- An SSE-S3 request without a master key or KMS is refused 400 (`rustfs@1.0.3:rustfs/src/storage/sse.rs:3588-3595`).
  rask's `vending.encryption` is empty by default (`chart/values.yaml:864-875`); the fixture sets no SSE and no bucket
  default encryption.
- SSE-C over plain HTTP is accepted by default (`RUSTFS_SSE_C_REQUIRE_TLS=false`,
  `rustfs@1.0.3:rustfs/src/server/ssec_transport.rs:56-57,107-132`). rask does not use SSE-C.

## Audit

Off by default (`RUSTFS_AUDIT_ENABLE`, `rustfs@1.0.3:crates/config/src/constants/env.rs:30-34`). Targets: webhook,
kafka, mqtt, mysql, postgres, nats, redis, amqp, pulsar; no OTLP audit target (`rustfs@1.0.3:crates/config/src/audit/mod.rs:41-53`).
Webhook delivery is at-least-once and probes the endpoint origin with `HEAD /`
(`rustfs@1.0.3:crates/targets/src/target/webhook.rs:90`). Records carry access keys, claims and headers: treat the
stream as sensitive. Reaching rask's Collector needs a webhook receiver there plus an `RUSTFS_OUTBOUND_ALLOW_ORIGINS`
entry; an open design choice.

## Surfaces to keep closed

- Console: on by default on `:9001` (`rustfs@1.0.3:crates/config/src/constants/app.rs:52,277-283`); the S3 listener
  never serves it (`rustfs@1.0.3:rustfs/src/startup_server.rs:332-349`). Off in the fixture.
- Console CORS: the image sets `*` (`rustfs@1.0.3:Dockerfile:115`); the compiled default is empty, same-origin
  (`rustfs@1.0.3:crates/config/src/constants/console.rs:27-34`). The docs env reference says the default is `*`
  (contradiction: `docs:reference/environment-variables.md`); true only of the image.
- FTP/FTPS/WebDAV/SFTP gateways: off unless enabled (`rustfs@1.0.3:rustfs/src/init.rs:947,1064,1185,1308`); each would
  accept an access/secret pair as a password. Leave all off.
- Iceberg REST catalog (`/iceberg/v1`, `/_iceberg/v1`): always registered, admin-gated, no switch found
  (`rustfs@1.0.3:rustfs/src/server/prefix.rs:58-73`). rask is Lance only; no rask policy grants `admin:*Table*`, and no
  rask bucket is a table bucket (a table bucket refuses mutation of reserved keys,
  `rustfs@1.0.3:rustfs/src/table_catalog/identifier.rs:443-461`).
- Bucket notifications: off (`RUSTFS_NOTIFY_ENABLE=false`, `env.rs:31,35`). rask's announcements come from the catalog's
  governed write path; a store event stream would be an ungoverned side channel.
- Update check: on by default (`console.rs:93-96`). Off.

## Hardening checklist: the fixture

- [ ] Root pair from ESO, mounted as files, `_FILE` env only; render fails on a missing Secret name.
- [ ] Console off; Service exposes 9000 only; no route through the Gateway.
- [ ] `RUSTFS_CHECK_UPDATE=false`, `RUSTFS_HEALTH_MINIMAL_RESPONSE_ENABLE=true`, notify/audit/gateways unset.
- [ ] Pod uid/gid/fsGroup 10001, read-only root, drop ALL, no SA token.
- [ ] NetworkPolicy admits only the components holding a store credential on 9000.
- [ ] The scoped-users hook is the only root holder besides the store and its dev seeder (the root's own OpenBao
      secret, denied to every Dapr app, and its own Kubernetes Secret); no `admin:*`, no `s3:ForceDelete*`, no table
      actions in any rask policy. In development the dev OpenBao's published root token reads every secret, the root's
      included, until XC-165 (#659) gives each app its own secret-store token and policy.
- [ ] Image pinned by digest at a GA tag.

## Platform store checklist

What rask demands of a production store, beyond the audit's list (R1-R11, R13, R15):

- No default root credential; root held by the platform only (STS tokens are signed with it).
- An explicit `RUSTFS_RPC_SECRET` (or the equivalent) on multi-node.
- TLS with a CA rask trusts; rask's vended endpoint is `https`.
- Console and admin API unreachable from workload namespaces, or reachable only by identities without `admin:*`.
- Path-style accepted, or the server domain named so rask can be configured for virtual-hosted style.
- `RUSTFS_DURABILITY_MODE=strict` (`operations.md`).
- Buckets unversioned, or versioned with a noncurrent-version expiry the platform owns; no Object Lock default
  retention; no expiration or transition lifecycle on rask prefixes except AbortIncompleteMultipartUpload.
- One writable site per table; replication only as a passive DR copy.
- Conditional writes proven exclusive across nodes (rask's CAS harness run against it).
- Encryption at rest from a KMS the platform owns, if required; SSE-C only with TLS required.
