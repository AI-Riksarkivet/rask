# RustFS configuration that matters to rask

Every row is read from source at `rustfs@1.0.3`, the pinned tag. Flags win over env (clap `env=` attributes,
`rustfs/src/config/cli.rs`). Boolean env is parsed strictly: write `true`/`false` (e.g. `RUSTFS_CHECK_UPDATE=off` stays
true, `rustfs@1.0.3:rustfs/src/init.rs:78-86`).

## Fixture env

| Variable | Fixture value | Default | Source |
|---|---|---|---|
| `RUSTFS_VOLUMES` | `/data` | required; image sets `/data` | `rustfs/src/config/cli.rs:1518-1524`; `Dockerfile:115-119` |
| `RUSTFS_ADDRESS` | `:9000` | `:9000` | `cli.rs:1527-1532`; `crates/config/src/constants/app.rs:269-273` |
| `RUSTFS_ACCESS_KEY_FILE` / `RUSTFS_SECRET_KEY_FILE` | mounted Secret files | unset (→ `rustfsadmin`) | `cli.rs:1551-1564`; `crates/credentials/src/constants.rs:21,28` |
| `RUSTFS_CONSOLE_ENABLE` | `false` | `true` | `cli.rs:1566-1571`; `app.rs:52` |
| `RUSTFS_CONSOLE_CORS_ALLOWED_ORIGINS` | `""` (if the console were on) | `""` in code, `*` in the image | `crates/config/src/constants/console.rs:27-34`; `Dockerfile:115` |
| `RUSTFS_CHECK_UPDATE` | `false` | `true` | `console.rs:93-96`; `rustfs/src/update.rs:101` |
| `RUSTFS_HEALTH_MINIMAL_RESPONSE_ENABLE` | `true` | `false` | `crates/config/src/constants/health.rs:65-66` |
| `RUSTFS_OBS_LOG_DIRECTORY` | `""` (stdout) | unset in code; `/logs` in the image | `crates/config/src/observability/mod.rs:61`; `crates/obs/src/config.rs:286-293`; `Dockerfile:118` |
| `RUSTFS_OBS_LOGGER_LEVEL` | `info` or `warn` | `error` in code; `warn` in the image | `app.rs:26-27`; `Dockerfile:117` |
| `RUSTFS_OBS_PROFILING_EXPORT_ENABLED` | `false` | `true` | `observability/mod.rs:48`; `app.rs:367-368` |
| `RUSTFS_OBS_ENDPOINT` | `http://<release>-otel-collector:4318` when observability is on | `""` | `cli.rs:1583-1590`; `app.rs:59` |
| `RUSTFS_OBS_USE_STDOUT` | `false` when OTLP is on | forced true with no endpoint | `observability/mod.rs:37`; `crates/obs/src/config.rs:273-285` |
| `RUSTFS_OBS_SERVICE_NAME` | `rustfs` | `RustFS` | `app.rs:20` |
| `RUSTFS_DURABILITY_MODE` | unset (strict) | strict | `crates/ecstore/src/disk/local.rs:1289-1296` |
| `RUSTFS_REGION` | unset (`us-east-1`) | `us-east-1` | `cli.rs:1600`; `app.rs:289-292` |
| `RUSTFS_SERVER_DOMAINS` | unset (path-style) | unset | `cli.rs:1535-1548` |
| `RUSTFS_ON_DEMAND_MIGRATION_ENABLED` | `false` (optional) | on; no outbound call unless a bucket has a source | `rustfs/src/module_switches.rs:41` |

## Leave unset (each opens a surface)

| Variable | Default | Source |
|---|---|---|
| `RUSTFS_FTP_ENABLE`, `RUSTFS_FTPS_ENABLE`, `RUSTFS_WEBDAV_ENABLE`, `RUSTFS_SFTP_ENABLE` | false | `rustfs/src/init.rs:947,1064,1185,1308` |
| `RUSTFS_NOTIFY_ENABLE`, `RUSTFS_AUDIT_ENABLE` | false | `crates/config/src/constants/env.rs:30-35` |
| `RUSTFS_KMS_ENABLE` | false | `cli.rs:1603-1638` |
| `RUSTFS_POLICY_PLUGIN_URL` | unset | `crates/config/src/opa/mod.rs:16` |
| `RUSTFS_UNSAFE_BYPASS_DISK_CHECK` (alias `MINIO_CI`) | false | `app.rs:131-140`; `crates/ecstore/src/layout/endpoints.rs:1596-1607` |
| `RUSTFS_KMS_ALLOW_INSECURE_DEV_DEFAULTS` | false; relaxes every KMS dev check at once | `cli.rs:1636`; `crates/kms/src/config.rs:26,1088,905-923` |
| `RUSTFS_HTTP3_ENABLE` | false; a QUIC (UDP) listener beside the TCP one | `crates/config/src/constants/tls.rs:212-215` |
| `RUSTFS_REPLICATION_ALLOW_LOOPBACK_TARGET` | false (SSRF guard) | `crates/ecstore/src/bucket/remote_s3_client.rs:428-434` |
| `RUSTFS_SSE_C_REQUIRE_TLS` | false; set true only if SSE-C is ever used | `rustfs/src/server/ssec_transport.rs:56-57` |

## Health and startup

| Variable | Default | Source |
|---|---|---|
| `RUSTFS_HEALTH_ENDPOINT_ENABLE` | true (false makes probes 404) | `health.rs:17-18` |
| `RUSTFS_HEALTH_READINESS_CACHE_TTL_MS` | 1000 | `health.rs:20-23` |
| `RUSTFS_HEALTH_CLUSTER_TIMEOUT_MS` | 2000 | `health.rs:37-40` |
| `RUSTFS_STARTUP_READINESS_MAX_WAIT_SECS` | 120 (`0` = default); startup fails past it | `health.rs:60-61`; `rustfs/src/server/readiness.rs:64-74,233-260` |
| `RUSTFS_HEALTH_PEER_READY_CHECK_ENABLE` | false | `health.rs:87-88` |
| `RUSTFS_HEALTH_COMPAT_BUSY_CHECK_ENABLE` | false (on: readiness may answer 429) | `health.rs:15-77`; `rustfs/src/server/layer.rs:1484-1499` |
| `RUSTFS_UNSUPPORTED_FS_POLICY` | `warn` (or `fail`) | `app.rs:188-197` |

## Identity, TLS, egress

| Variable | Default | Source |
|---|---|---|
| `RUSTFS_IDENTITY_OPENID_{ENABLE,CONFIG_URL,ISSUER,CLIENT_ID,CLIENT_SECRET,SCOPES,OTHER_AUDIENCES,REDIRECT_URI,REDIRECT_URI_DYNAMIC,CLAIM_NAME,CLAIM_PREFIX,ROLE_POLICY,DISPLAY_NAME,GROUPS_CLAIM,ROLES_CLAIM,EMAIL_CLAIM,USERNAME_CLAIM,HIDE_FROM_UI}[_<ID>]` | provider exists only with CONFIG_URL; groups claim `groups`; scopes `openid,profile,email` | `crates/config/src/constants/oidc.rs:35-52`; `crates/iam/src/oidc/config.rs:566-602,644-737` |
| `RUSTFS_OUTBOUND_ALLOW_ORIGINS` | empty; private/loopback origins refused | `crates/utils/src/egress.rs:25-29,362-372` |
| `RUSTFS_TLS_PATH` | `""` (plain HTTP); files `rustfs_cert.pem`, `rustfs_key.pem` | `cli.rs:1593-1594`; `app.rs:220,264` |
| `RUSTFS_TLS_RELOAD_ENABLE` / `_INTERVAL` | false / 30 s | `crates/config/src/constants/tls.rs:198-209` |
| `RUSTFS_SERVER_MTLS_ENABLE` | false | `tls.rs:86` |
| `RUSTFS_TRUSTED_PROXY_NETWORKS` | `127.0.0.1,::1` | `crates/config/src/constants/proxy.rs:19-69` |
| `RUSTFS_RPC_SECRET` | derived from root; fails closed on a default half | `crates/credentials/src/constants.rs:34`; `crates/credentials/src/credentials.rs:256-273` |

## Data-plane knobs (production, informational)

| Variable | Default | Source |
|---|---|---|
| `RUSTFS_API_STALE_UPLOADS_EXPIRY` / `_CLEANUP_INTERVAL` | 24 h / 6 h | `crates/ecstore/src/bucket/lifecycle/bucket_lifecycle_ops.rs:184-188` |
| `RUSTFS_STORAGE_CLASS_INLINE_BLOCK` | 128 KiB | `crates/ecstore/src/config/storageclass.rs:84-103` |
| `RUSTFS_ERASURE_SET_DRIVE_COUNT` | 0 (auto) | `crates/ecstore/src/layout/disks_layout.rs:24-26` |
| `RUSTFS_SCANNER_SPEED` / `RUSTFS_SCANNER_CYCLE` | `default` / 60 s | `crates/config/src/constants/scanner.rs:136-173,306-311` |
| `RUSTFS_HEAL_AUTO_HEAL_ENABLE` | true | `crates/config/src/constants/heal.rs:84-115` |

## MINIO_* aliases

A fixed allowlist of `MINIO_*` variables is copied to `RUSTFS_*` at startup when the canonical name is unset
(`crates/utils/src/envs.rs:124-185,715-730`). The docs say "the IDENTITY_OPENID_* keys"; only nine are mapped, so for
example `MINIO_IDENTITY_OPENID_OTHER_AUDIENCES` is dropped (contradiction: `docs:reference/environment-variables.md`).
rask writes canonical `RUSTFS_*` names only.
