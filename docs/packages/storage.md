# packages/storage

A small, **Ray-free** seam over S3-compatible stores: the boto3 S3 client factory, the STS client, key listing with
timestamped-prefix retention, S3 URI parsing, and the `StorageError` family every S3 call answers in.

**Protocol-agnostic on purpose.** Nothing here knows one workload's source format or where a workload reads its bytes
from: a workload's sources and sinks live in that workload (the HTR runner's filesystem, S3 and IIIF sources are in
`runners/htr/src/htr`).

→ Auto-generated symbol docs: **[API reference](../reference/storage.md)**.

## S3 client (`s3_client`)

`s3_client(endpoint=None)` builds a boto3 client for any S3-compatible store:

- **path-style addressing** (required by RustFS unless its server domains are set),
- s3v4 signing, adaptive retries,
- checksum calculation/validation set to `when_required`,
- 10s connect / 60s read timeouts.

It derives no credential: a key pair (and a vended session token) is passed per client, or the standard AWS chain
supplies one. A lone `session_token` without its key pair is refused, because botocore would drop it and sign with the
env chain.

**TLS precedence:** a custom CA (`RASK_S3_CA_BUNDLE`, else `S3_CA_BUNDLE`) wins;
else insecure (`RASK_S3_INSECURE`, `S3_INSECURE` or `HCP_INSECURE` set to
`1`/`true`/`yes`) sets `verify=False` and silences urllib3 warnings; else system
trust.

## Listing and errors

`iter_keys(client, bucket, prefix, suffix)` pages `list_objects_v2`, and `prune_timestamped_prefixes` keeps the newest
N timestamped prefixes under a base. Every S3 call is wrapped in `s3_errors`, so a caller meets
`BucketNotFoundError` / `ObjectNotFoundError` and never a raw botocore `ClientError`.

`split_s3_uri("s3://bucket/prefix/")` returns `("bucket", "prefix/")`.
