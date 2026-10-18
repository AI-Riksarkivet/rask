# The S3 surface rask uses on RustFS

Status words: **measured** (rask's live runs on 1.0.1 and 1.0.3, `docs/audits/2026-10-18/rustfs-zero-trust.md` § Measured),
**gated** (in RustFS's own s3-tests list at `rustfs@1.0.3:scripts/s3-tests/implemented_tests.txt`), **source** (read,
not run), **open** (neither).

| Operation | Who uses it in rask | Status | Evidence |
|---|---|---|---|
| PutObject `If-None-Match: *` → 412, one winner under contention | Lance commit (put-if-not-exists, `lance_docs/file_format.md` § Commit Protocol), registry create (`packages/service-kit/src/service_kit/lakehouse/records.py:95-111`), the catalog's warehouse validate probe (`services/catalog/src/catalog/api/v1/endpoints/warehouses.py:1289-1291`) | measured (16×20 race) + gated | `implemented_tests.txt:355-363`; lock re-check `rustfs@1.0.3:crates/ecstore/src/set_disk/ops/object.rs:4715-4728` |
| PutObject `If-Match: <etag>` | registry read-modify-write (`records.py:235-256`) | measured + gated | `rustfs@1.0.3:crates/ecstore/src/set_disk/core/io_primitives.rs:6425-6431` |
| CompleteMultipartUpload conditional | Lance large commit | gated + source | `implemented_tests.txt:470-472`; `rustfs@1.0.3:crates/ecstore/src/set_disk/ops/multipart.rs:2702-2742` |
| Multipart from pyarrow 25 (`STREAMING-UNSIGNED-PAYLOAD-TRAILER`) | record store, on a service's own scoped pair (`packages/service-kit/src/service_kit/lakehouse/record_store.py:54-61`) | measured on 1.0.1 and 1.0.3; refused by alpha.60 | `.dagger/storage.go`; audit § Measured |
| HeadBucket (pyarrow's bucket check before a write) | no rask path through a vended credential | measured: refused to a prefix-scoped session | `rustfs@1.0.3:rustfs/src/storage/access.rs:2955-2960` |
| Multipart from Lance's object_store | Lance data files | measured (create + append) | audit § Measured |
| Ranged GET | Lance reads | measured implicitly (reads after append); multi-page offsets not isolated | audit § Measured |
| ListObjectsV2 prefix/delimiter | Lance listing, sweep | measured (prefix-scoped list) + gated | `implemented_tests.txt` |
| DeleteObjects (≤1,000 keys) | warehouse purge (`services/catalog/src/catalog/services/warehouses.py:605-630`), sweep | gated; purge not measured | limit `rustfs@1.0.3:rustfs/src/app/object/delete.rs:471-474` |
| CreateBucket / DeleteBucket | warehouse lifecycle | open (purge path not traced) | `rustfs@1.0.3:crates/ecstore/src/store/bucket.rs:36-86` |
| STS AssumeRole + session token on every call | vending | measured | `rustfs@1.0.3:rustfs/src/admin/handlers/sts.rs:212-333` |
| AssumeRoleWithWebIdentity | `vending.mode: web_identity` | source | `sts.rs:399-441` |
| HEAD of a missing key under a prefix-scoped policy | Lance probes | open: `test_head_object_404_with_policy_prefix` is unimplemented (403 possible where AWS answers 404) | `rustfs@1.0.3:scripts/s3-tests/unimplemented_tests.txt` |
| Conditional DeleteObject (`If-Match`) | none found in `records.py` | excluded from the gate | `rustfs@1.0.3:scripts/s3-tests/excluded_tests.txt:138-156` |
| CopyObject with destination conditions | none | source: parsed and checked; atomicity under contention not traced | `rustfs@1.0.3:rustfs/src/storage/options.rs:476-495` |

## Limits

- Single PUT and UploadPart ≤ 5 GiB, refused from Content-Length before the body
  (`rustfs@1.0.3:crates/config/src/constants/body_limits.rs:73`).
- Parts 1..10,000; every part but the last ≥ 5 MiB (`rustfs@1.0.3:crates/ecstore/src/set_disk/mod.rs:344,7640`).
- ListParts `max-parts` above 1,000 is refused 400, not clamped as on AWS
  (`rustfs@1.0.3:rustfs/src/storage/s3_api/multipart.rs:105`).
- Object keys with a `.`/`..` segment, `//`, NUL, or over 32 KiB are refused
  (`rustfs@1.0.3:crates/ecstore/src/bucket/utils.rs:109-162`). rask's key builders (`merge_prefix`, warehouse
  prefixes) must never produce `//`.
- An upload must carry Content-Length or an aws-chunked decoded length; a proxy that re-chunks bodies breaks uploads
  (`rustfs@1.0.3:rustfs/src/app/object/put.rs:111-128`).
- Storage classes: only STANDARD and REDUCED_REDUNDANCY (`rustfs@1.0.3:rustfs/src/app/object/extract.rs:1610-1623`).

## Addressing and region

- Path-style unless `RUSTFS_SERVER_DOMAINS` is set (`rustfs@1.0.3:rustfs/src/config/cli.rs:1535-1548`). rask: Lance
  `virtual_hosted_style_request=false`, boto3 `addressing_style=path`, pyarrow's path-style default for an
  `endpoint_override` (`packages/service-kit/src/service_kit/lakehouse/objectfs.py:37,45`; `records.py:63`). The
  objectfs docstring's "RustFS rejects virtual-hosted" is true only while no server domain is set.
- Region defaults to `us-east-1` (`rustfs@1.0.3:crates/config/src/constants/app.rs:289-292`). Enforcement of the
  signing region lives in the `s3s` dependency and is not traced; "RustFS ignores region" on the integration pages is
  unverified.
- `allow_http` is derived from the endpoint scheme in rask (`objectfs.py:49-62`): plain HTTP in dev, TLS in production.

## Not used by rask, and not to be relied on

ACL authorization (canned headers partly accepted, XML grants `NotImplemented`, GetBucketAcl synthetic,
`rustfs@1.0.3:rustfs/src/storage/s3_api/acl.rs:18-31`); bucket access logging and ownership controls (planned); POST
object checksums; S3 Select on compressed input (`docs:administration/protocols/s3.md`). The SDK index's "fully
compatible" (`docs:developer/sdk/index.md`) is a claim, not a guarantee; rask's guarantees are its measured lanes.
