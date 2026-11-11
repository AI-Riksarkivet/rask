"""Shared object-store plumbing for the services: pyarrow-filesystem resolution + Lance storage options.

One home for the ``storage_options`` → ``pyarrow.fs`` translation that the outbox, the warehouse registry,
the dataset-relocation path and the compaction sweep all need — and
for building the lance-style ``storage_options`` itself, so the path-style/allow-http keys can never
drift per-service (audit 2026-07-15: compaction's hand-rolled copy had already dropped
``virtual_hosted_style_request``). An ``s3://`` root builds an ``S3FileSystem`` from the lance-style
options (endpoint/keys/region, path-style, scheme derived from the endpoint); anything else (a ``file://``
or bare local path — dev/tests) resolves via the local filesystem, so every consumer round-trips without
object storage in unit tests.

The options travel as :class:`~lakehouse.storage_options.StorageOptions`, whose key pair is ``SecretStr`` ([[LH-543]]),
and become the flat map only at the call that signs with them, through that guard's own S3 filesystem builder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import pyarrow.fs as pafs
from pydantic import BaseModel, Field

from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.storage_options import REGION, VIRTUAL_HOSTED, StorageOptions, on_s3, s3_filesystem


if TYPE_CHECKING:
    from collections.abc import Callable


def lance_storage_options(
    endpoint: str,
    key_pair: S3KeyPair,
    region: str,
    *,
    virtual_hosted: bool = False,
    server_side_encryption: str | None = None,
    sse_kms_key_id: str | None = None,
    sse_bucket_key_enabled: bool | None = None,
) -> StorageOptions:
    """The lance-style ``storage_options`` every service opens datasets with, signed by ``key_pair``.

    Path-style addressing is the default (``virtual_hosted=False``): RustFS rejects virtual-hosted
    signing with 403 ``SignatureDoesNotMatch``, and one omitted key in a hand-rolled copy is exactly the
    drift this builder exists to prevent.

    EVERY KEY IS SPELLED AS LANCE READS THE ENVIRONMENT INTO IT (``aws_endpoint``, ``aws_region``,
    ``aws_virtual_hosted_style_request``), so an ambient ``AWS_ENDPOINT_URL``, ``AWS_REGION`` or
    ``AWS_VIRTUAL_HOSTED_STYLE_REQUEST`` cannot stand in for it ([[LH-238]]; the measurement is at
    :data:`lakehouse.storage_options._OPTION_ALIASES`), and a named endpoint is also set as the S3 service's own
    (:data:`lakehouse.storage_options.S3_ENDPOINT`), which ``AWS_ENDPOINT_URL_S3`` would otherwise fill and which outranks
    it. An empty endpoint (AWS proper) names no endpoint at all: the client resolves the regional one, and an empty value
    would pin it to nothing.

    ``allow_http`` IS DERIVED FROM THE ENDPOINT'S SCHEME and cannot be passed: ``"true"`` exactly for an ``http`` scheme,
    in any case. It is a PERMIT — "Allow non-TLS, i.e. non-HTTPS connections" (``lance_docs/lance/guide/object_store.md:32``)
    — that object_store consults only when a request would otherwise be refused, so a TLS store holding it looks
    identical until something downgrades. Measured on pylance 12.0.0 against a closed port, in a process with no ambient
    ``AWS_ALLOW_HTTP``: ``http://`` with ``allow_http=false`` dies at client construction (``builder error``),
    ``https://`` with ``allow_http=false`` proceeds to its TLS request. An ambient ``AWS_ALLOW_HTTP`` beats the key in
    every spelling (measured on pylance 13.0.0: 10 of 10 fresh processes sent plaintext over ``allow_http=false``, 8 of
    10 over ``aws_allow_http=false``), and settles nothing once the endpoint is pinned: the request goes to the endpoint
    the options name, over the scheme that endpoint names. The rule is ``lakehouse.endpoint_scheme.allow_http_for``, which
    the catalog's ``namespace_properties`` calls too.

    The key pair's session token completes a VENDED credential. An STS credential is a triple and the token is the
    half that carries the scoping, so a builder that cannot express one forces every vended-credential
    caller to hand-roll the map — the drift above — or to sign as a different identity.

    The credential keys are spelled ``aws_``-prefixed by :meth:`~lakehouse.storage_options.StorageOptions.in_clear`,
    which says why the bare spellings lose to the environment.

    Unknown keys are the hazard this builder stands between callers and: object_store silently IGNORES
    a storage option it does not recognise (verified 2026-09-03 — an invented key produced no error and
    no change to the signed request), so a mis-spelled credential field is dropped with nothing to
    notice. Pinned on the wire by ``test_a_vended_credential_survives_the_storage_seam.py`` and by
    ``test_explicit_credentials_beat_the_ambient_environment.py``.
    """
    options = {
        REGION: region,
        # AWS proper, until an endpoint is named: the regional endpoint the client resolves is TLS.
        "allow_http": "false",
        VIRTUAL_HOSTED: str(virtual_hosted).lower(),
        **_encryption_options(server_side_encryption, sse_kms_key_id, sse_bucket_key_enabled),
    }
    at_aws = StorageOptions(key_pair=key_pair, options=options)
    return at_aws.with_endpoint(endpoint) if endpoint else at_aws


class StoreTimeouts(BaseModel):
    """How long one object-store call may take before Lance gives up, as ``storage_options`` keys.

    A PLANE CHOOSES THESE, which is why they are a value here and not baked into
    :func:`lance_storage_options`: a request-serving plane must fail fast and let its caller retry,
    while a batch plane would rather wait out a slow store than fail a long job. Without them Lance runs
    on object_store's defaults (`lance_docs/lance/guide/object_store.md:28-43`: connect 5 s, request 30 s, 3 retries
    within 180 s), and MEASURED on pylance 12.0.0 one dataset open against a store that drops packets
    took **124.9 s**, and against one that accepts the connection and never answers it outlasted a
    300 s harness.

    THE REQUEST KEY IS ``timeout`` (`lance_docs/lance/guide/object_store.md:37`), and an unrecognised key is ignored
    rather than refused. Measured on pylance 12.0.0 against a store that accepts and never answers: with
    ``request_timeout=5s`` each request still ran its full 30.0 s, with ``timeout=3s`` it ran 3.0 s.
    ``connect_timeout``, ``client_max_retries`` and ``client_retry_timeout`` were honoured as
    documented.

    LANCE RETRIES ABOVE THE CLIENT, so the bound on one call is a multiple of these. Measured on 12.0.0:
    a dataset open against a silent store issued its version listing **six times**, with
    ``client_max_retries=0`` and with the AIMD throttle's ``lance_aimd_max_retries=0`` alike, so one
    open costs about six times ``(client_max_retries + 1) * request`` plus a few hundred milliseconds of
    backoff.
    """

    model_config = {"frozen": True}

    connect_s: float = Field(gt=0)
    request_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    retry_window_s: int = Field(ge=0)

    def storage_options(self) -> dict[str, str]:
        """The four keys, in the spelling object_store honours (milliseconds, so a fraction survives), to set over a plane's
        :class:`StorageOptions` (:meth:`StorageOptions.with_options`)."""
        return {
            "connect_timeout": f"{round(self.connect_s * 1000)}ms",
            "timeout": f"{round(self.request_s * 1000)}ms",
            "client_max_retries": str(self.max_retries),
            "client_retry_timeout": str(self.retry_window_s),
        }


#: The three algorithms object_store accepts, verbatim from `lance_docs/lance/guide/object_store.md:134`. A fourth value
#: is not an error at the store — it is an ignored option, and the bytes land in plaintext.
_SSE_ALGORITHMS: Final = ("AES256", "aws:kms", "aws:kms:dsse")
#: The two that carry a customer key. `lance_docs/lance/guide/object_store.md:135`, on `aws_sse_kms_key_id`: "If set,
#: `aws_server_side_encryption` must be `"aws:kms"` or `"aws:kms:dsse"`."
SSE_KMS_ALGORITHMS: Final = ("aws:kms", "aws:kms:dsse")


def _encryption_options(algorithm: str | None, kms_key_id: str | None, bucket_key_enabled: bool | None) -> dict[str, str]:
    """Encryption-at-rest options, REFUSING the combinations the store would silently ignore (§ J5).

    Validated here rather than trusted, because this module's whole reason for existing is that
    object_store DROPS an option it does not recognise without erroring (probed 2026-09-03). For
    credentials that surfaces later as a permission failure; for encryption it surfaces as nothing at
    all — the write succeeds, unencrypted, under a configuration that claims otherwise. There is no
    later signal to catch it, which is why the check belongs at the only point that sees both fields.
    """
    if kms_key_id and algorithm not in SSE_KMS_ALGORITHMS:
        raise ValueError(
            f"aws_sse_kms_key_id requires aws_server_side_encryption to be one of {SSE_KMS_ALGORITHMS} (got {algorithm!r}) — "
            "the store would ignore the key id and encrypt with its default, so the key would appear configured and never be used"
        )
    if algorithm is not None and algorithm not in _SSE_ALGORITHMS:
        raise ValueError(
            f"aws_server_side_encryption must be one of {_SSE_ALGORITHMS} (got {algorithm!r}) — any other value is dropped and the data lands unencrypted"
        )
    options: dict[str, str] = {}
    if algorithm:
        options["aws_server_side_encryption"] = algorithm
    if kms_key_id:
        options["aws_sse_kms_key_id"] = kms_key_id
    if bucket_key_enabled is not None:
        options["aws_sse_bucket_key_enabled"] = str(bucket_key_enabled).lower()
    return options


def fs_and_base(root_uri: str, storage_options: StorageOptions) -> tuple[pafs.FileSystem, str]:
    """Resolve ``(filesystem, base_path)`` for ``root_uri`` (base has no scheme and no trailing slash).

    A root on S3 (:func:`~lakehouse.storage_options.on_s3`) is reached signed by the options' key pair, at their endpoint
    or, naming none, at AWS proper's regional one: pyarrow's ``FileSystem.from_uri`` would sign with the ambient credential
    chain and follow an ambient ``AWS_ENDPOINT_URL`` (measured on pyarrow 25.0.0), so a record written under the pair would
    be read under another identity ([[LH-572]]).
    """
    if on_s3(root_uri):
        fs = s3_filesystem(storage_options, allow_bucket_creation=True)
        return fs, root_uri[len("s3://") :].rstrip("/")
    resolved, path = pafs.FileSystem.from_uri(root_uri)
    return resolved, path.rstrip("/")


def is_lance_dataset_root(uri: str, storage_options: StorageOptions) -> bool:
    """Does a Lance DATASET live at ``uri``, as the object store sees it?

    The estate's one definition of the marker, shared rather than re-spelled: a directory IS a
    dataset iff it has a ``_versions/`` child (``maintenance.services.discovery.discover_datasets``
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
    :func:`lakehouse.features.gather_compaction_bases`.
    """
    fs, base = fs_and_base(uri, storage_options)
    return fs.get_file_info(f"{base}/_versions").type == pafs.FileType.Directory


def same_store_uri(reference_uri: str, stated_path: str) -> str:
    """``stated_path``, as a MANIFEST states it, respelled in ``reference_uri``'s store.

    A Lance manifest stores a base path VERBATIM in the spelling it was created with: a branch, clone,
    multi-base or configured base on an S3 dataset all carry their ``s3://`` scheme (measured pylance
    12.0.0 over moto, lh279 r3 b_store), so the base a real dataset declares already names its store and
    same-scheme is returned unchanged. The respelling exists for a base stated WITHOUT a scheme — no
    catalog door writes one, so it is defensive: handed straight to a resolver a scheme-less path reads
    as a LOCAL absolute path (measured, same run), which does not exist, which a dataset-root probe
    answers False to — a wrong PERMIT, and the one direction the compaction gate must never take. So a
    scheme-less base is respelled into the reference dataset's store (fail-closed: probe where a real
    root could be) rather than stripped.

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
        base = same_store_uri(dataset_uri, stated_path)
        return any(is_lance_dataset_root(spelling, storage_options) for spelling in _where_a_root_may_be(base))

    return probe


def _where_a_root_may_be(uri: str) -> list[str]:
    """``uri``, and for an ``s3://`` spelling the key it decodes to as well.

    pylance opens a URL at each segment percent-decoded once, while :func:`fs_and_base` keeps an S3 store's
    ``s3://`` spelling literal, so a base declared in the catalog's encoded spelling (``…$r%C3%A4ksm%C3%B6rg%C3%A5s``) is
    a dataset root at the decoded key (``…$räksmörgås``) and nothing at the spelled one ([[LH-319]]); pyarrow decodes a
    ``file://`` URI itself. A root found at either refuses compaction, so probing both never permits more.
    """
    from lakehouse.base_refs import decoded_path  # base_refs imports this module

    if not on_s3(uri):
        return [uri]
    opened = f"s3://{decoded_path(uri)}"
    return [uri] if opened == uri.rstrip("/") else [uri, opened]
