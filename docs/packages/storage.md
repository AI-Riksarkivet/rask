# packages/storage

A small, **Ray-free** abstraction over three byte stores — the local filesystem,
S3/HCP buckets, and the Riksarkivet IIIF Image API — behind a uniform
Source/Sink shape. It also bundles the boto3 S3 client factory tuned for HCP and
the HCP credential derivation.

→ Auto-generated symbol docs: **[API reference](../reference/storage.md)**.

## The Source/Sink contract

There is **no base class** — Source/Sink is a duck-typed structural contract:

- **Source** — `keys() -> Iterable[str]` and `read(key) -> bytes`
- **Sink** — `existing_keys(suffix="") -> Iterable[str]` and `write(key, data)`

| Implementation | Module | Notes |
|---|---|---|
| `FSSource` / `FSSink` | `fs.py` | Local filesystem; `FSSink.__init__` eagerly creates its root. |
| `S3Source` / `S3Sink` | `s3.py` | S3/HCP; keyword-only; needs `client` (tests) or `client_factory` (picklable Ray runs). `S3Sink` defaults `content_type="application/xml"` (ALTO). |
| `IIIFCachedSource` | `iiif.py` | Read-through cache: S3 first, IIIF on miss, write-through to the cache bucket. |

`build_source(uri, …)` / `build_sink(uri, …)` (in `uri.py`) dispatch on the URI:
`s3://…` → S3 with an `s3_client` factory; anything else → filesystem.

## S3 client (`s3_client`)

`s3_client(endpoint=None)` builds a boto3 client tuned for HCP/MinIO:

- **path-style addressing** (required by HCP/MinIO),
- s3v4 signing, adaptive retries (legacy S3 retry handler unregistered),
- checksum calculation/validation set to `when_required` (HCP compatibility),
- 10s connect / 60s read timeouts.

**TLS precedence:** `HCP_CA_BUNDLE` (custom CA) wins; else `HCP_INSECURE`
(`1`/`true`/`yes`) sets `verify=False` and silences urllib3 warnings; else system
trust. HCP serves a self-signed cert, so dev usually sets `HCP_INSECURE`.

## HCP credential derivation

`derive_hcp_creds()` populates the standard AWS env vars from HCP tenant
credentials, by HCP's fixed convention:

- `AWS_ACCESS_KEY_ID = base64(HCP_USERNAME)`
- `AWS_SECRET_ACCESS_KEY = md5(HCP_PASSWORD)` (hex)

It is a **no-op unless both** `HCP_USERNAME` and `HCP_PASSWORD` are set, and it
**never overwrites** pre-existing `AWS_*` vars. It is not called by `s3_client` —
callers invoke it explicitly at startup (the runner and viewer both do).

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
