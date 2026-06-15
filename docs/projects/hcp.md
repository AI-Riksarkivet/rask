# HCP (Storage Backend)

!!! warning "Not a deployable project"
    Despite appearing in the nav and older docs alongside `runner`/`viewer`,
    **there is no `projects/hcp`**. "HCP" is the **Hitachi Content Platform** —
    the S3-compatible object-storage backend rask reads and writes. It is
    implemented in [`packages/storage`](../packages/storage.md) and configured
    entirely through environment variables.

## What it is

HCP is the production object store. All image, ALTO, and search data lives in
HCP buckets, accessed through boto3 with HCP-specific tuning. In local
development the same code can point at **MinIO** instead.

## Configuration

| Variable | Purpose |
|---|---|
| `HCP_ENDPOINT` | S3 endpoint URL (e.g. `https://dev-ai.hcp.ra-dev.int`). |
| `HCP_USERNAME` / `HCP_PASSWORD` | Tenant credentials; the `AWS_*` pair is derived from these. |
| `HCP_INSECURE` | Skip TLS verification (HCP uses a self-signed cert). |
| `HCP_CA_BUNDLE` | Alternative: a custom CA bundle path (takes precedence over `HCP_INSECURE`). |

### Credential derivation

`derive_hcp_creds()` sets the standard AWS env vars from the HCP convention:
`AWS_ACCESS_KEY_ID = base64(HCP_USERNAME)`, `AWS_SECRET_ACCESS_KEY =
md5(HCP_PASSWORD)`. It is a no-op unless both are set and never overwrites
existing `AWS_*`.

### Client tuning

`s3_client()` configures **path-style addressing**, s3v4 signing, adaptive
retries, and `when_required` checksums — the combination HCP (and MinIO) need.
See [packages/storage](../packages/storage.md#s3-client-s3_client) for the full
TLS precedence and gotchas.

## Buckets

| Bucket (default) | Role |
|---|---|
| `images-batch` | Input image cache (IIIF read-through target). |
| `images-batch-alto` | ALTO XML output. |
| `images-batch-search` | Lance `lines` + `archive_catalog` tables (search). |

!!! danger "Self-signed TLS bites two clients differently"
    boto3 honours `HCP_INSECURE` with `verify=False`; **LanceDB's Rust S3 client**
    needs `allow_invalid_certificates` in its storage options. Both are wired
    from `HCP_INSECURE` — without them you get `SSLError` (boto3) or
    `error sending request` (Lance).
