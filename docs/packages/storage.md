# lakehouse.storage

How a lakehouse process reaches its object store and holds its session there (`packages/lakehouse/src/lakehouse/storage`,
ADR 0224): the boto3 S3 client factory (`client`), the STS client (`sts`), the `s3://` split and join (`uri`), the
`StorageError` family the library's S3 calls answer in (`errors`), the key pair a process signs with (`credential`), its
storage options and the pyarrow filesystem they build (`options`, `endpoint_scheme`, `objectfs`),
the session its ServiceAccount token is exchanged for (`store_session`), objects listed and deleted by key
(`stored_objects`), and the JSON records the store arbitrates with `If-None-Match` and `If-Match` (`conditional_records`).
The package re-exports nothing: an importer names the module that defines what it uses.

**Protocol-agnostic on purpose.** Nothing here knows one workload's source format or where a workload reads its bytes
from: a workload's sources and sinks live in that workload, outside the lakehouse.

→ Auto-generated symbol docs: **[API reference](../reference/storage.md)**.

## S3 client (`s3_client`)

`s3_client(endpoint=None, *, key_pair)` builds a boto3 client for any S3-compatible store:

- **path-style addressing** (required by RustFS unless its server domains are set),
- s3v4 signing, adaptive retries,
- checksum calculation/validation set to `when_required`,
- 10s connect / 60s read timeouts.

It derives no credential and reads none from its surroundings: it signs with the `S3KeyPair` it is passed (its session
token too, for a session or a vend) and nothing else. A pair without its key id or its secret is refused with
`MissingStoreCredentialError`, and `object_client` and `s3_filesystem` refuse storage options that carry no whole pair,
because botocore and pyarrow would otherwise sign with their default chain (environment variables, shared files,
profiles, a credential process, container or instance metadata). Every client is built from one boto3 session that reads
no profile and no shared file, so no `AWS_PROFILE` or config file the environment names changes what a client is.

**TLS precedence:** a custom CA (`RASK_S3_CA_BUNDLE`) wins; else insecure (`RASK_S3_INSECURE` set to
`1`/`true`/`yes`) sets `verify=False` and silences urllib3 warnings; else system trust.

## Listing and errors

Every S3 call the library makes is wrapped in `lakehouse.storage.errors.store_call`, so its caller meets the `StorageError`
family for whatever the store answered and for a call that got no answer: `ObjectNotFoundError` (also a
`FileNotFoundError`), `BucketNotFoundError`, `PreconditionFailedError` (412: a conditional write whose condition did not
hold), `WriteConflictError` (409: another write of the key was in flight, which the store has not settled),
`StoreRefusedError` and `StoreUnansweredError`. The family is `OSError`, the type pyarrow and Lance raise for the same
failures, and each error chains the client's own as its cause; `no_answer` is the one rule for which failure is an
outage, which `lakehouse.problems.outage` reads. A lost race (`RecordExistsError`, `RecordChangedError`) and an absent
record (`RecordMissingError`) are not in the family: as `OSError`s every `except OSError` would answer them as an outage.
A defect is not in the family either: a missing key pair (`MissingStoreCredentialError`) is no `OSError`, and a failure
of the client's that is no answer of the store's (a parameter it refuses) passes through `store_call` as itself. The
backups' key listing and their retention walk (`prune_timestamped_prefixes`, which keeps the newest N timestamped
prefixes under a base) are the backup scripts' own, in `scripts/_backup_store.py`.

`split_s3_uri("s3://bucket/prefix/")` returns `("bucket", "prefix")`, and `s3_object("s3://bucket/prefix", "k")` returns
`("bucket", "prefix/k")`.
