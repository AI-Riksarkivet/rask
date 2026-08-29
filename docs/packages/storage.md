# packages/storage

A small, **Ray-free** abstraction over two byte stores — the local filesystem and
S3-compatible buckets — behind a uniform Source/Sink shape. It also bundles the
boto3 S3 client factory and the HCP credential derivation.

**Protocol-agnostic on purpose.** A read-through cache for one image API lived here
until 2026-08-17 with exactly one consumer and moved to `runners/htr`: a shared
package carrying a single workload's protocol is how that workload becomes
privileged.

→ Auto-generated symbol docs: **[API reference](../reference/storage.md)**.

## The Source/Sink contract

`storage.protocol` states it, as two runtime-checkable Protocols — structural, so an
adapter that lives in a caller satisfies them without importing anything from here:

- **Source** — `keys() -> Iterable[str]` and `read(key) -> bytes`
- **Sink** — `existing_keys(suffix="") -> Iterable[str]` and `write(key, data)`

| Implementation | Module | Notes |
|---|---|---|
| `FSSource` / `FSSink` | `fs.py` | Local filesystem; `FSSink.__init__` eagerly creates its root. |
| `S3Source` / `S3Sink` | `s3.py` | S3; keyword-only; needs `client` (tests) or `client_factory` (picklable Ray runs). Both inherit the lazy-client/pickle behaviour from one `_LazyClientAdapter`. `S3Sink` defaults `content_type="application/xml"`. |

Every S3 call is wrapped in `s3_errors`, so a caller meets `BucketNotFoundError` /
`ObjectNotFoundError` and never a raw botocore `ClientError`.

`build_source(uri, …)` / `build_sink(uri, …)` (in `uri.py`) dispatch on the URI:
`s3://…` → S3 with an `s3_client` factory; anything else → filesystem.

## S3 client (`s3_client`)

`s3_client(endpoint=None)` builds a boto3 client tuned for HCP/MinIO:

- **path-style addressing** (required by HCP/MinIO),
- s3v4 signing, adaptive retries,
- checksum calculation/validation set to `when_required` (HCP compatibility),
- 10s connect / 60s read timeouts.

**TLS precedence:** a custom CA (`RASK_S3_CA_BUNDLE`, else `S3_CA_BUNDLE`) wins;
else insecure (`RASK_S3_INSECURE`, `S3_INSECURE` or `HCP_INSECURE` set to
`1`/`true`/`yes`) sets `verify=False` and silences urllib3 warnings; else system
trust. HCP serves a self-signed cert, so dev usually sets `HCP_INSECURE`.

## HCP credential derivation

`derive_hcp_creds()` **returns** `{access_key, secret_key}` derived from HCP tenant
credentials by HCP's fixed convention, or `None`:

- `access_key = base64(HCP_USERNAME)`
- `secret_key = md5(HCP_PASSWORD)` (hex)

It returns `None` unless both `HCP_USERNAME` and `HCP_PASSWORD` are set, and also
when an `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` is already set — so MinIO,
rustfs and real AWS are left untouched.

It is **pure: it never writes to `os.environ`**, which is what lets one process
address more than one backend. `s3_client` applies it per client (`client.py:98`),
so callers do not invoke it themselves — a bare call is dead code, and anything
building a raw `boto3.client` must pass the returned pair explicitly or it will run
unauthenticated. Legacy — drop this bridge once off HCP.

## IIIF read-through cache

`IIIFCachedSource.read(key)` tries S3 first; on a `NoSuchKey`/`404` miss it
fetches from IIIF (`{base}/arkis!{id}/full/max/0/default.jpg`) with exponential
backoff and writes the bytes back to the cache bucket. **Non-404 S3 errors
propagate** (so an `AccessDenied` doesn't trigger an IIIF stampede); **cache-write
failures are swallowed** (the read still returns the fetched bytes). The default
timeout is a deliberate 15s so one sticky URL can't freeze an actor for minutes.

## Gotchas

- **Picklability drives the factory pattern** — build S3/IIIF sources with a
  module-level callable or `functools.partial` (not a lambda); `__getstate__`
  nulls the live client so it rebuilds on the worker.
- **md5 password hashing is intentional** (HCP convention, not security).
- **`S3Sink` content type is `application/xml`** and `build_sink` exposes no
  override; the IIIF write-through hardcodes `image/jpeg` separately.
- **No formal Protocol** — a new backend just needs the right method names.
