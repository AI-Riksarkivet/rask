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

from typing import TYPE_CHECKING

import pyarrow.fs as pafs


if TYPE_CHECKING:
    from collections.abc import Callable


StorageOptions = dict[str, str]


def lance_storage_options(
    endpoint: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    *,
    allow_http: bool = True,
    virtual_hosted: bool = False,
    session_token: str | None = None,
) -> StorageOptions:
    """The lance-style ``storage_options`` dict every service opens datasets with.

    Path-style addressing is the default (``virtual_hosted=False``): RustFS/MinIO reject virtual-hosted
    signing with 403 ``SignatureDoesNotMatch``, and one omitted key in a hand-rolled copy is exactly the
    drift this builder exists to prevent.

    ``session_token`` completes a VENDED credential. An STS credential is a triple and the token is the
    half that carries the scoping, so a builder that cannot express one forces every vended-credential
    caller to hand-roll the dict — the drift above — or to sign as a different identity. It is omitted
    entirely when absent rather than set empty: object_store treats a present-but-empty token as a
    token and the request is refused.

    Unknown keys are the hazard this builder stands between callers and: object_store silently IGNORES
    a storage option it does not recognise (verified 2026-09-03 — an invented key produced no error and
    no change to the signed request), so a mis-spelled credential field is dropped with nothing to
    notice. ``session_token`` is object_store's own spelling, pinned on the wire by
    ``test_a_vended_credential_survives_the_storage_seam.py``.
    """
    options: StorageOptions = {
        "endpoint": endpoint,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "region": region,
        "allow_http": str(allow_http).lower(),
        "virtual_hosted_style_request": str(virtual_hosted).lower(),
    }
    if session_token:
        options["session_token"] = session_token
    return options


def s3_filesystem(storage_options: StorageOptions, *, allow_bucket_creation: bool = False) -> pafs.S3FileSystem:
    """An ``S3FileSystem`` from lance-style storage options — scheme derived from the endpoint (an
    ``https://`` endpoint keeps TLS; hardcoding ``http`` once silently downgraded a secured connection).

    The ``session_token`` is forwarded for the same reason ``records._s3_client`` forwards it, and the
    stakes are higher here: pyarrow falls back to the default credential chain for anything it was not
    given, so a half-forwarded vended credential can sign with the POD's own role — broader rights than
    the catalog scoped, not narrower. Dropping it fails open.
    """
    scheme, _, host = storage_options["endpoint"].partition("://")
    return pafs.S3FileSystem(
        access_key=storage_options.get("access_key_id"),
        secret_key=storage_options.get("secret_access_key"),
        session_token=storage_options.get("session_token"),
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


def is_lance_dataset_root(uri: str, storage_options: StorageOptions) -> bool:
    """Does a Lance DATASET live at ``uri``, as the object store sees it?

    The estate's one definition of the marker, shared rather than re-spelled: a directory IS a
    dataset iff it has a ``_versions/`` child (``maintenance.services.optimize.discover_datasets``
    walks on exactly this, and ``purge`` / ``reconcile`` probe the same path). Cheap — one
    ``get_file_info`` per call, no listing.

    It exists because a MANIFEST cannot be trusted to say this about somebody else's directory:
    ``BasePath.is_dataset_root`` is set by ``shallow_clone`` and by nothing else, so an ``add_bases``
    pointed straight at a live Lance root reports ``False`` (measured, pylance 10.0.0). The listing
    does not lie, and the compaction gate reads it.

    RAISES rather than returning False when the store cannot answer. "Unreadable" and "not a dataset"
    are opposite answers for a gate that must fail closed, and collapsing them here would hand the
    caller a confident False it has no way to question. A path that simply is not there is not an
    unreadable one — it answers False, which is an OBSERVATION (nothing is at that prefix) and not a
    guess; a base whose root has been deleted is still caught by the ``DataFile.base_id`` reading in
    :func:`service_kit.lakehouse.features.gather_compaction_bases`.
    """
    fs, base = fs_and_base(uri, storage_options)
    return fs.get_file_info(f"{base}/_versions").type == pafs.FileType.Directory


def same_store_uri(reference_uri: str, stated_path: str) -> str:
    """``stated_path``, as a MANIFEST states it, respelled in ``reference_uri``'s store.

    A Lance manifest records a base path as "interpretable by the object store", which on S3 is the
    schemeless ``/bucket/ns/t.lance`` while the caller holds ``s3://bucket/ns/t.lance`` — the two
    spellings :func:`service_kit.lakehouse.base_refs.normalise` exists to reconcile for COMPARISON.
    A probe cannot use that one: it has to hand a filesystem something it can actually resolve.

    Handing the schemeless form straight to a resolver reads it as a LOCAL absolute path, which does
    not exist, which a dataset-root probe answers False to — a wrong PERMIT, and the one direction the
    compaction gate must never take. Hence a respelling rather than a strip.

    RAISES when the two spellings cannot be reconciled (a base stated in a different scheme than the
    dataset's own store). Unknown has to reach the gate AS unknown; a best guess here is the same
    wrong permit wearing a different shape.
    """
    scheme, sep, _ = reference_uri.partition("://")
    stated_scheme, stated_sep, _ = stated_path.partition("://")
    if not sep:
        if stated_sep:
            raise ValueError(f"base {stated_path!r} names a store the dataset at {reference_uri!r} does not live in")
        return stated_path
    if not stated_sep:
        return f"{scheme}://{stated_path.lstrip('/')}"
    if stated_scheme != scheme:
        raise ValueError(f"base {stated_path!r} names a store the dataset at {reference_uri!r} does not live in")
    return stated_path


def dataset_root_probe(dataset_uri: str, storage_options: StorageOptions) -> Callable[[str], bool]:
    """:func:`is_lance_dataset_root`, bound to the store the dataset at ``dataset_uri`` lives in.

    The binding is the point: a base path comes off a manifest in the manifest's spelling, and the
    respelling (:func:`same_store_uri`) must happen between the two or the probe answers about the
    wrong filesystem. Raises out of the returned callable rather than swallowing — the caller
    (``features.gather_compaction_bases``) records the failure as the unknown it is.
    """

    def probe(stated_path: str) -> bool:
        return is_lance_dataset_root(same_store_uri(dataset_uri, stated_path), storage_options)

    return probe
