# 0166. A secret read from the store is re-read on a TTL; a service's own S3 key pair is held in the store (2026-10-16)

**Status:** accepted; amended by [ADR 0170](0170-the-catalogs-store-identity-is-its-serviceaccount.md), under which the
catalog holds no key pair: its store identity is its ServiceAccount token. Source: owner, 2026-10-16, two decisions on
XC-082 (#215) through the multi-question tool: where a service's own S3 key id lives (in the store with its secret,
Lakekeeper's model), and which tests pin the row (the real apps over a moto server that enforces key ids). Reference: Lakekeeper's secret cache
(`crates/lakekeeper/src/service/secrets.rs`, `service/cache_ttl.rs`) and credential update
(`api/management/v1/warehouse/mod.rs`, `update_storage_credential`), read at `0b3fb120`.

## Context

Every pod read the secret store once. The catalog, lineage and maintenance spliced their S3 secret into their cached
settings at boot (`apply_dapr_secrets`), the catalog cached each data base's credential reference for the life of
the process (`warehouse_credentials.resolve`, an `lru_cache`), and every sidecar-delivered door cached the Dapr app
token the same way. A credential rotated in OpenBao therefore reached no running pod, and once the old one was
revoked every call under it failed until somebody restarted the fleet: a store the pods never re-read makes a
rotation an outage (criterion 5).

The key id half was configuration (`LANCE_S3_ACCESS_KEY_ID`, rendered from `minio.<plane>AccessKey`). Every S3-family
store rotates by issuing a NEW key id: AWS cannot change a key's secret, and MinIO changing a user's secret kills the
old one at once. With the id in configuration, a rotation either paired the new secret with the old id or needed a
chart change and a restart.

Measured on pylance 13.0.0 over a moto server enforcing key ids: once its key is revoked, a `dir` namespace
connection built with it answers every call with the store's refusal, while a connection built with the new key
works, and so does an open through the same `lance.Session` the old key opened with. So a fix that re-reads the
store is not enough on its own: everything built once at boot that holds a key must be rebuilt, or must read the key
at each use.

## Decision

1. **Every value read from the store is served for at most 600 s, jittered downward to between 300 and 600 s, then
   read again** (`service_kit.governed.secret_cache.STORE_READS`). Loads are single-flight per (store, secret,
   fields), and a load that fails, or a field the store does not hold, is never cached and never extends what is
   held, so a value the store stopped confirming is dropped once its TTL passes (fail closed). This is Lakekeeper's
   shape: a 600 s TTL with downward jitter, single-flight loads, no negative caching.
2. **A service's own S3 key pair lives in the store, both halves**: `<plane>-s3-access-key` beside
   `<plane>-s3-secret-key` in `secret/lance` (the root pair is `minio-access-key` and `minio-secret-key`), named by
   `*_DAPR_SECRET_S3_ID_FIELD` and `*_DAPR_SECRET_S3_FIELD`. The chart renders no `*_S3_ACCESS_KEY_ID` in store
   mode. A bundle carrying only one half is refused: half a credential signs nothing.
3. **The pair is read at boot and kept confirmed in the background** (`service_kit.governed.store_credential`).
   `storage_options()` is built on the event loop at nearly every request, so it never reads the store: it peeks at
   what the cache holds and answers 503 when the store has not confirmed the pair within its TTL. A background loop
   reads the pair again whenever half its TTL is used, so a rotation is served within about five minutes and a
   store outage shorter than half a TTL refuses nobody. A data base's credential reference, read in worker threads
   beside the dataset opens that need it, is read on demand through the same cache.
4. **Nothing built at boot keeps a pair.** The catalog's namespace connections are keyed on the pair they were built
   with, so the first request after a rotation builds anew; the STS vendor reads the pair at each vend and rebuilds
   its client when it changed (Lakekeeper keys its STS cache on the credential's hash); the base judge is installed
   as a builder; the outbox emitters take `storage_options` as a callable read at each stage; maintenance builds its
   bucket-listing client on each reconcile tick.
5. **The Dapr app token accepts the current or the previous token, and rotates by restart.** daprd reads its token
   from the ESO-synced `<release>-dapr-app-token` once, when its pod starts, so a sidecar presents the old token
   until a restart, and one that restarts before ESO syncs still starts with it. A store-mode door therefore accepts
   `dapr-app-token` and `dapr-app-token-previous`, both read through the same TTL, so a token removed from the store
   stops being accepted with no restart.

## Considered options

- **Keep the key id in configuration and re-read only the secret.** Smaller, with no change to what the store holds.
  Rejected: a rotation could then only change the secret of a fixed id, which MinIO turns into an outage until each
  pod's next read and AWS IAM does not allow at all. Lakekeeper never splits the pair either.
- **Read the store on demand everywhere, as Lakekeeper does.** Lakekeeper's reads are async. rask's `storage_options()`
  is a sync call on the event loop at about a hundred sites, so a read on demand would block the loop for each pod's
  re-read and, while the store is down, for every request. The pair is kept confirmed in the background instead;
  the credential references, read in worker threads, stay on demand.
- **Validate a new pair against the bucket before adopting it, as Lakekeeper's credential update does.** Lakekeeper
  owns the write, through its management API. rask has no write API for its own credential, since the operator
  writes OpenBao directly, so the check is a runbook step before the write (docs/OPERATORS.md § 8).
- **Serve a held value past its TTL while the store is down.** Keeps the lakehouse up through a long store outage,
  but serves a credential the store no longer confirms, and removing a key from the store would then not stop its
  use. Rejected, as Lakekeeper rejects it.

## Consequences

- A rotation is: issue a second key, check it against the bucket, write the pair to the store, wait ten minutes,
  revoke the first. No restart, and no window in which a pod signs with a revoked key (docs/OPERATORS.md § 8).
- The catalog's pair signs its STS `AssumeRole`, which MinIO refuses from an access key (measured live 2026-10-16:
  `AccessDenied`, every vend 503 until the store named a user's pair again, with no restart either way). On MinIO
  the catalog therefore rotates to a second user under the same policy; AWS lets any key of the IAM user assume the
  role.
- The platform's store holds every service's key id beside its secret (docs/architecture/deployment.md); the dev
  seed writes them.
- A secret store that stays down for longer than a held value's TTL makes the lakehouse answer 503 on every request
  that signs an object-store call. The secret store's availability is now part of the lakehouse's.
- The app-token rotation keeps `dapr-app-token-previous` until every sidecar has restarted; clearing it earlier
  refuses the deliveries of a sidecar still presenting it, once the TTL passes.
- The media plane's own process-lifetime caches (`service_kit.media.config._store_secret`, the viewer's per-store
  credentials) are not changed: that plane is outside Phase 1.
