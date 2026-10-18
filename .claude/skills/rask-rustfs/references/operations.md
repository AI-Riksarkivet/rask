# Operating RustFS

Citations: `rustfs@1.0.3:<path>:<line>`, `docs:<path>`, rask paths bare.

## Health

- Seven unauthenticated probe paths on `:9000`, GET and HEAD: `/health`, `/health/live`, `/minio/health/live`
  (liveness); `/health/ready`, `/minio/health/ready` (node readiness); `/minio/health/cluster` (write quorum, pool
  metadata, locks); `/minio/health/cluster/read` (`rustfs@1.0.3:rustfs/src/server/prefix.rs:30-49`;
  `rustfs@1.0.3:rustfs/src/server/layer.rs:1435-1446`). They run before signature checking and are exempt from the
  readiness gate and the rate limiter (`rustfs@1.0.3:rustfs/src/server/http.rs:2321-2348`;
  `rustfs@1.0.3:rustfs/src/server/readiness.rs:126-152`; `rustfs@1.0.3:rustfs/src/server/rate_limit.rs:390-406`).
- Liveness is local: 200 even before startup finishes (`rustfs@1.0.3:rustfs/src/server/health.rs:108-124`).
- Until startup is fully ready, every non-probe request gets 503 with `Retry-After: 5` and `x-rustfs-readiness-pending`
  (`readiness.rs:150-190`). A restarting store looks like 503s, not refused connections.
- Node readiness = read quorum + IAM + lock read quorum, withdrawn on stalled object stages
  (`RUSTFS_HEALTH_OBJECT_PROGRESS_ENABLE`, default true); peer health only with
  `RUSTFS_HEALTH_PEER_READY_CHECK_ENABLE=true` (default false); KMS only with
  `RUSTFS_HEALTH_COMPAT_KMS_READY_CHECK_ENABLE=true` (`readiness.rs:867-956,787-795`;
  `rustfs@1.0.3:crates/config/src/constants/health.rs:24-34,79-88`). Write quorum is not part of it. The docs say
  readiness needs peer health (contradiction: `docs:operations/status-check.md` vs `health.rs:86-88`).
- Cluster probes are cached, single-flighted and bounded by `RUSTFS_HEALTH_CLUSTER_TIMEOUT_MS` (2,000), answering 503
  on timeout (`readiness.rs:1028-1080`). `/health/ready` is the lighter, steadier readiness probe.
- Probe bodies carry the server version and dependency detail unless `RUSTFS_HEALTH_MINIMAL_RESPONSE_ENABLE=true`
  (`health.rs:378-420`; `health.rs` constants `:65-66`). On in the fixture.
- Startup fails if not ready within `RUSTFS_STARTUP_READINESS_MAX_WAIT_SECS` (120 s) (`health.rs:60-61`;
  `readiness.rs:233-260`).
- For rask: liveness probe `/health/live`, readiness `/health/ready`, chaos drill "store is back" = `GET /health/ready`
  returns 200, as `tests/e2e-py/chaos.py` probes it.

## Observability

- Telemetry is OTLP/HTTP protobuf push only, never gRPC, never a scrape: `RUSTFS_OBS_ENDPOINT` is a base URL, the
  exporter appends `/v1/traces`, `/v1/metrics`, `/v1/logs` (`rustfs@1.0.3:crates/obs/src/telemetry/otel.rs:20-33,150-161,424-480`;
  `rustfs@1.0.3:rustfs/src/config/cli.rs:1583-1590`). Default empty: no export. No `/metrics` route exists. The
  Thanos page scrapes `rustfs:9000/metrics` (contradiction: `docs:developer/integration/observability/thanos.md`).
- In rask: `RUSTFS_OBS_ENDPOINT=http://<release>-otel-collector:4318` when `observability.enabled`, which fits the one
  telemetry seam. The OTLP exporter does not go through the outbound egress guard (no `egress` reference under
  `rustfs@1.0.3:crates/obs/src`, grep), so a private Collector address needs no `RUSTFS_OUTBOUND_ALLOW_ORIGINS` entry.
  With OTLP log export on, logs go to OTLP only, so `kubectl logs` shows none (`otel.rs:181-203`).
- Log routing, in order: any OTLP endpoint → OTLP pipeline (file logs only if OTLP log export is unavailable); else a
  non-empty `RUSTFS_OBS_LOG_DIRECTORY` → rolling files; else stdout JSON
  (`rustfs@1.0.3:crates/obs/src/telemetry/mod.rs:33-41`; `otel.rs:181-196`). The stdout mirror defaults to
  `!is_production` (`rustfs@1.0.3:crates/obs/src/telemetry/local.rs:78-80`). The image sets a log directory and
  production (`rustfs@1.0.3:Dockerfile:115-119`), so it prints nothing to stdout by default.
- Docs contradictions: logs are not written to file "additionally" beside OTLP (`docs:operations/observability.md` vs
  `otel.rs:192-196`); a `://` value in the log directory is not a remote sink (`rustfs@1.0.3:crates/obs/src/config.rs:286-293`);
  `RUSTFS_OBS_LOG_ROTATION_SIZE_MB` is never read (`rustfs@1.0.3:crates/config/src/constants/app.rs:320-325`).
- A bad log path: the docs say startup exits; the source falls back to stdout on permission denied only
  (`local.rs:558-567`). A read-only filesystem error is neither; leave the directory empty in the fixture.
- `RUSTFS_OBS_USE_STDOUT` adds stdout span/metric exporters, forced on with no OTLP endpoint
  (`crates/obs/src/config.rs:273-285`). Set it false whenever OTLP is on, or spans are re-ingested by filelog.
- Profiling export defaults to on and warns without a Pyroscope endpoint (`app.rs:367-368`; `otel.rs:553-590`). Off.
- Sampler `ParentBased(TraceIdRatio(RUSTFS_OBS_SAMPLE_RATIO))`, W3C propagation, so the caller's decision governs
  (`otel.rs:377-396`). Name the service with `RUSTFS_OBS_SERVICE_NAME`.
- Metric names confirmed in source: `rustfs_runtime_readiness_ready`, `rustfs_runtime_readiness_degraded_total`
  (`readiness.rs:75-76`), `rustfs_start_total` (`otel.rs:297`), `rustfs_scanner_objects_scanned_total`,
  `rustfs_scanner_directories_scanned_total` (`rustfs@1.0.3:crates/obs/src/metrics/collectors/scanner.rs:593-594`),
  `rustfs_bucket_quota_check_failed_total`, `rustfs_bucket_delete_blockers_total`. The scanner page's three metric
  names do not exist (contradiction: `docs:administration/data/object/scanner.md`). An alert rule in
  `deploy/alerting/rules.yml` uses a name read from source or a live export, never a docs page.

## Upgrades

- The docs give no on-disk compatibility, mixed-version or downgrade guarantee (`docs:operations/upgrade/index.md`);
  CHANGELOG carries per-feature caveats (`rustfs@1.0.3:CHANGELOG.md:22,59`). Every pin bump is re-measured.
- rask's discipline: one tag for fixture and Dagger (`.dagger/storage.go:15`), pinned by digest; a bump re-runs the
  CAS, STS and Lance lanes in the same commit; never delete the PVC to "fix" an upgrade.

## Scaling and healing (platform)

- Capacity grows only by appending a pool; pools are append-only; rebalance and decommission need more than one pool
  (`docs:operations/cluster-lifecycle.md`; `rustfs@1.0.3:crates/ecstore/src/store/init_format.rs:720-740`).
- Auto-heal on by default, 4 concurrent heals (`rustfs@1.0.3:crates/config/src/constants/heal.rs:84-115`). EC:0
  cannot heal: on the fixture a lost volume is lost data.
- Scanner: default cycle 60 s, deep bitrot every 30 days, heal selection 1 in 1,024
  (`rustfs@1.0.3:crates/config/src/constants/scanner.rs:169,282-287`;
  `rustfs@1.0.3:crates/scanner/src/scanner_folder.rs:100-102`). The docs say "once daily" (contradiction:
  `docs:administration/data/object/scanner.md`). Lifecycle, replication and usage accounting are scanner-driven and lag.
- Durability: `RUSTFS_DURABILITY_MODE=strict|relaxed|none`, default strict
  (`rustfs@1.0.3:crates/ecstore/src/disk/local.rs:1250-1261,1473-1502`). Lance manifests are small and often inlined in
  `xl.meta` (inline below 128 KiB, `rustfs@1.0.3:crates/ecstore/src/config/storageclass.rs:84-103`), so anything
  below strict can lose a committed version on power loss. Strict everywhere a kill drill runs.

## Data features against Lance

- **Versioning**: a delete on a versioned bucket writes a marker and frees nothing; the inline budget is divided by 8
  (`storageclass.rs:243-256`). Lance keeps its own versions; maintenance's sweep, Lance cleanup and the warehouse purge
  free nothing on a versioned bucket. rask buckets stay unversioned.
- **Object Lock**: needs versioning; a locked version blocks Lance cleanup, the orphan sweep, trash purge and warehouse
  purge with AccessDenied (`docs:administration/data/object/object-lock.md`, not checked in source). Never on rask buckets.
- **Lifecycle**: an expiration rule over a Lance prefix deletes fragments a manifest references and corrupts the table
  (`docs:administration/data/lifecycle-management.md`). Only AbortIncompleteMultipartUpload is safe.
- **Stale multipart uploads** are aborted in the background after 24 h, every 6 h
  (`rustfs@1.0.3:crates/ecstore/src/bucket/lifecycle/bucket_lifecycle_ops.rs:184-188`). Not in the docs.
- **Quota**: a hard byte cap refused as `InvalidRequest "Bucket quota exceeded"`; a checker fault fails CLOSED with 503
  (`rustfs@1.0.3:rustfs/src/app/object/shared.rs:198-262`). The docs say it degrades to allow (contradiction:
  `docs:administration/data/bucket/quota.md`). rask sets no quota; if a platform does, the catalog maps the refusal to a
  typed error.
- **Replication**: asynchronous, one-way, needs versioning on both ends (`docs:administration/data/bucket/replication.md`).
  Per-object async copies have no cross-object order, so a replica can hold a manifest whose data files have not
  landed (inference): not a consistent Lance backup, and two writable sites would let two commits win.
- **Delete bucket**: refused `BucketNotEmpty` (409) from a raw per-disk residue scan, logged and counted
  (`rustfs@1.0.3:crates/ecstore/src/store/bucket.rs:36-86,103`). Whether pending multipart uploads count as residue is
  not confirmed; the purge aborts them before DeleteBucket and treats a 409 after a full drain as a store fault.
