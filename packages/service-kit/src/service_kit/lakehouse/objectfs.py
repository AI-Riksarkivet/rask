"""Shared object-store plumbing for the services: pyarrow-filesystem resolution + Lance storage options.

One home for the ``storage_options`` → ``pyarrow.fs`` translation that the outbox, the warehouse registry,
the model registry, the dataset-relocation path, the compaction sweep, and the media head all need — and
for building the lance-style ``storage_options`` dict itself, so the path-style/allow-http keys can never
drift per-service (audit 2026-07-15: compaction's hand-rolled copy had already dropped
``virtual_hosted_style_request``). An ``s3://`` root builds an ``S3FileSystem`` from the lance-style
options (endpoint/keys/region, path-style, scheme derived from the endpoint); anything else (a ``file://``
or bare local path — dev/tests) resolves via the local filesystem, so every consumer round-trips without
object storage in unit tests.
"""

from __future__ import annotations

import pyarrow.fs as pafs


StorageOptions = dict[str, str]


def lance_storage_options(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    *,
    allow_http: bool = True,
    virtual_hosted: bool = False,
) -> StorageOptions:
    """The lance-style ``storage_options`` dict every service opens datasets with.

    Path-style addressing is the default (``virtual_hosted=False``): RustFS/MinIO reject virtual-hosted
    signing with 403 ``SignatureDoesNotMatch``, and one omitted key in a hand-rolled copy is exactly the
    drift this builder exists to prevent.
    """
    return {
        "endpoint": endpoint,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "region": region,
        "allow_http": str(allow_http).lower(),
        "virtual_hosted_style_request": str(virtual_hosted).lower(),
    }


def s3_filesystem(storage_options: StorageOptions, *, allow_bucket_creation: bool = False) -> pafs.S3FileSystem:
    """An ``S3FileSystem`` from lance-style storage options — scheme derived from the endpoint (an
    ``https://`` endpoint keeps TLS; hardcoding ``http`` once silently downgraded a secured connection)."""
    scheme, _, host = storage_options["endpoint"].partition("://")
    return pafs.S3FileSystem(
        access_key=storage_options.get("access_key_id"),
        secret_key=storage_options.get("secret_access_key"),
        endpoint_override=host or storage_options["endpoint"],
        scheme=scheme or "http",
        region=storage_options.get("region", ""),
        allow_bucket_creation=allow_bucket_creation,
    )


def fs_and_base(root_uri: str, storage_options: StorageOptions) -> tuple[pafs.FileSystem, str]:
    """Resolve ``(filesystem, base_path)`` for ``root_uri`` (base has no scheme and no trailing slash)."""
    if root_uri.startswith("s3://") and storage_options.get("endpoint"):
        fs = s3_filesystem(storage_options, allow_bucket_creation=True)
        return fs, root_uri[len("s3://") :].rstrip("/")
    resolved, path = pafs.FileSystem.from_uri(root_uri)
    return resolved, path.rstrip("/")
