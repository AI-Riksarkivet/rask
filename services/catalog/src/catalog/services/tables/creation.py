"""Bringing a table into being: a declaration reserves and registers a table's location, and :func:`create_table` is the
one facade that receives the raw Arrow-IPC ``data`` and writes it directly at file format 2.2 with stable row ids — the
durable row identity row-id lineage needs."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress

import lance
import pyarrow as pa
from lance_namespace import (
    ConcurrentModificationError,
    CreateTableResponse,
    DeregisterTableRequest,
    DescribeTableRequest,
    DropTableRequest,
    InternalError,
    InvalidInputError,
    LanceNamespace,
    NamespaceExistsRequest,
    NamespaceNotFoundError,
    RegisterTableRequest,
    TableAlreadyExistsError,
    TableNotFoundError,
)
from pydantic import BaseModel

from catalog.core import encoding, native, primary_keys, provenance_guard
from catalog.core.config import shared_lance_session
from catalog.core.lance_refusals import clean_lance_message
from catalog.core.modes import CreateMode
from catalog.core.namespace import disarm_commit_path_cleanup
from catalog.core.provenance_stamp import declare_dataset_id
from catalog.core.table_objects import RESERVED_MARKER, dataset_fs, delete_table_object, holds_a_version
from catalog.services import commit_authors, committed_files, contracts, table_bases, table_claims
from lakehouse.lancekit.base_refs import decoded_path
from lakehouse.lancekit.features import CREATE_FILE_VERSION, CREATE_FORMAT, manifest_base_path_refs
from lakehouse.lancekit.lance_session import open_dataset
from lakehouse.problems.ns_errors import PartiallyAppliedError
from lakehouse.process.supervision import is_fault
from lakehouse.records import base_registry, commit_records, file_identities, location_claims
from lakehouse.storage.options import StorageOptions, object_client, on_s3
from lakehouse.storage.uri import split_s3_uri


__all__ = [
    "DataBase",
    "create_table",
    "declare_table",
    "existing_location",
    "kept_table",
    "registered_data_bases",
    "stands_at",
    "table_data_bases",
]


log = logging.getLogger(__name__)


def _base_name(uri: str) -> str:
    """A stable, deterministic base NAME for a #3-B data-distribution base URI.

    ``target_bases`` references bases by name; deriving the name from the URI (not a random id) means a create
    and any later op agree byte-for-byte, and the manifest's registered-base names stay human-readable."""
    return uri.replace("s3://", "").strip("/").replace("/", "-") or "base"


class DataBase(BaseModel):
    """A data base as ONE table registers it: that table's own directory under an approved base ([[LH-252]])."""

    #: ``<approved base>/<table directory>`` — the path the manifest records and every read resolves through.
    path: str
    #: The name ``target_bases`` routes fragments by, derived from the APPROVED base so a re-sent
    #: ``data_base`` names the same registered base on every write.
    name: str


def table_data_bases(approved: Sequence[str], directory: str) -> list[DataBase]:
    """The data bases a CREATE registers: one directory per approved base, all named ``directory``.

    A TABLE'S OWN DIRECTORY, never the approved base itself. Registered at the base, every table naming
    it round-robined its fragments into one flat ``<base>/``, and a vend for one table granted READ on
    ``<base>/*`` — every sibling's fragments. Under ``<base>/<directory>/`` a table's files are a prefix
    no other table writes, so the session policy can grant exactly that prefix. Measured on pylance
    12.0.0: ``DatasetBasePath(<base>/<dir>, is_dataset_root=False)`` writes its fragments under
    ``<dir>/`` and reads them back (``lance_docs/lance/format/table/layout.md`` § Base Path System: a non-root base "points
    directly to the file directory").
    """
    approved = list(dict.fromkeys(approved))
    names = [_base_name(uri) for uri in approved]
    # _base_name is lossy (s3://b/a/c and s3://b/a-c both → b-a-c). A collision would silently make one
    # approved base unaddressable + make target resolution ambiguous — reject it loudly, never misroute.
    if len(set(names)) != len(names):
        raise InvalidInputError(f"data_base paths collide on base name {names}; use distinct base paths")
    return [DataBase(path=f"{uri.rstrip('/')}/{directory}", name=name) for uri, name in zip(approved, names, strict=True)]


def registered_data_bases(approved: Sequence[str], dataset: lance.LanceDataset) -> list[DataBase]:
    """The data bases an OVERWRITE targets: each approved base's directory as the table's manifest registered it.

    Bases register at create only, so the names a re-sent ``data_base`` derives must match a base the
    manifest already holds; an approved base this table never registered is refused here rather than
    reaching pylance as an unknown target name.
    """
    registered = {ref.name: ref.path for ref in manifest_base_path_refs(dataset) if ref.name}
    bases: list[DataBase] = []
    for uri in dict.fromkeys(approved):
        name = _base_name(uri)
        if name not in registered:
            raise InvalidInputError(f"data_base {uri!r} is not registered on this table; a table's data bases register when it is created")
        bases.append(DataBase(path=registered[name], name=name))
    return bases


def _write_blob(
    table: pa.Table,
    uri: str,
    so: StorageOptions,
    *,
    mode: str,
    external_blob_bases: list[str],
    data_bases: list[DataBase] | None = None,
    properties: dict[str, str] | None = None,
    transaction_properties: dict[str, str] | None = None,
) -> lance.LanceDataset:
    """Write a table at file format 2.2 with stable row ids.

    ``external_blob_bases`` registers the base the table's external pointers must lie under
    (lance_docs/lance/guide/blob.md); a ``Blob.from_uri`` pointer outside every registered base is refused. ``data_bases``
    (#3-B) are the table's own directories under approved DATA-distribution bases, which the fragments
    round-robin across (the Uber pattern): :func:`table_data_bases` on a create,
    :func:`registered_data_bases` on an overwrite, which reuses the bases the table registered at create."""
    is_create = mode == "create"
    table = encoding.stamp_encoding(table, properties)
    data_bases = data_bases or []
    # DatasetBasePath registers each base (is_dataset_root=False = a raw data location, not a nested
    # dataset). initial_bases REGISTERS the bases in the manifest — CREATE-only (an overwrite/append reuses
    # the already-registered set; re-registering on overwrite is rejected by pylance).
    external_paths = [lance.DatasetBasePath(b, is_dataset_root=False) for b in external_blob_bases]
    data_paths = [lance.DatasetBasePath(base.path, is_dataset_root=False, name=base.name) for base in data_bases]
    _has_bases = bool(external_blob_bases or data_bases)
    initial_bases = (external_paths + data_paths) if is_create and _has_bases else None
    # target_bases is the WRITE TARGET: it round-robins the FRAGMENT writes across the data bases while the
    # manifest + _versions stay in the primary root (relative-path portable — a relocation moves the base
    # URIs, not 10M file paths). Applied on ANY mode when data_bases is SUPPLIED, so a re-supplied overwrite
    # ALSO distributes. CAVEAT: a mutation that does NOT re-send data_base (a bare overwrite, or the
    # /insert append route which has no data_base param) concentrates its NEW fragments in the primary root —
    # create-time distribution is the firm guarantee; per-write distribution needs the bases re-supplied.
    target_bases = [base.name for base in data_bases] or None
    # Every base writes under the root's own options, Lance's default (`lance_docs/lance/guide/object_store.md` § Per-Base
    # Configuration: "every base inherits the unscoped options"): a data base on the estate's store is the table's own
    # storage, reached under the catalog's identity as the root is ([[LH-286]]).
    try:
        return lance.write_dataset(  # noqa: TID251
            table,
            uri,
            mode=mode,
            storage_options=so.in_clear(),
            data_storage_version=CREATE_FILE_VERSION,
            # #5a: durable row identity — ``_rowid`` stays constant across compaction (which rewrites
            # fragments and invalidates row ADDRESSES), gating the row-id index + row-version tracking
            # (``_row_*_at_version``) that field-level/temporal lineage rests on. CREATE-TIME-ONLY: an ordinary
            # write with it on does not migrate an existing dataset (`lance_docs/lance/format/table/row_id_lineage.md`
            # § Enabling Stable Row IDs), so it is set on every create and overwrite here.
            enable_stable_row_ids=CREATE_FORMAT.stable_row_ids,
            initial_bases=initial_bases,
            target_bases=target_bases,
            # Never ([[LH-410]]): a pointer outside every registered base is one its writer chose freely, which the blob
            # door would read with the catalog's own identity and network position.
            allow_external_blob_outside_bases=False,
            transaction_properties=transaction_properties,
        )
    except OSError as exc:
        # An external-pointer blob outside every registered base (and the blanket flag off) is a client
        # error, not a 500 — surface a clear 400. Match lance's specific phrase (NOT a bare "external"), so a
        # genuine infra OSError on a path that merely contains the word "external" still surfaces as a 500.
        if "outside registered external bases" in str(exc).lower():
            raise InvalidInputError(
                "blob column references an external object outside the dataset root and any registered "
                "external base; name the base on the create (external_blob_base), inside a base "
                "LANCE_EXTERNAL_BLOB_BASES approves for the table's warehouse"
            ) from exc
        # A CREATE THAT LANDS ON OCCUPIED BYTES IS A CONFLICT, NOT AN INTERNAL FAILURE
        # ([[LH-164]]). `spec.yaml:1461` declares `ConflictErrorResponse` on `CreateTable`, so a
        # collision is a modelled outcome of this operation. Measured on the live catalog 2026-09-21:
        # `POST /v1/table/silver$features/create` answered 500 `detail: "Internal Server Error"` while
        # the reason — `Dataset already exists: s3://bind86-wh/medallion/silver` — existed only in the
        # pod's traceback.
        #
        # THE DETAIL NAMES THE LOCATION AND DENIES THE TABLE, because those bytes are ungoverned
        # residue at the path the catalog COMPOSES for this id, not a table record. "Table already
        # exists" would send the caller looking for something that does not exist; the location is the
        # one actionable fact, and it is exactly what the 500 withheld.
        #
        # Matches lance's specific phrase for the same reason the branch above does: a translation
        # that swallowed every OSError would relabel genuine infra failures as client errors.
        if "dataset already exists" in str(exc).lower():
            raise TableAlreadyExistsError(
                f"a dataset already occupies {uri}, and the catalog holds no table record for it — "
                "so it is storage residue rather than a governed table. Clear or relocate that "
                "location, or create under an id that composes to a different one"
            ) from exc
        raise


def declare_table(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    *,
    root: str,
    claims: location_claims.ClaimStore | None,
    holder: str,
    table_uuid: str | None = None,
    before_register: Callable[[str, str], None] | None = None,
    after_register: Callable[[], None] | None = None,
) -> str:
    """Declare ``segments`` at ``<root>/<table uuid>``, where a table the catalog makes lives, and answer that location ([[LH-520]]).

    The `dir` catalog's own DeclareTable steps (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:132-149`)
    with the catalog choosing the location, as the spec leaves it to (`lance_docs/ns_catalog/spec.yaml:3831-3835`). The
    backend's DeclareTable puts the name in the path (`<hash>_<object_id>`) and refuses any other location (measured on
    pylance 13.0.0), so ADR 0173 §5 takes the steps here, in this order:

    1. the table's uuid is minted (``table_uuid``, when the caller minted it) and its location's claim taken under it,
       before any backend call, so the location is this table's before anything resolves to it;
    2. ``before_register`` is handed the uuid and the location: a governed door writes the table's grants there, so they stand before the
       table does ([[LH-588]]) and the claim already records the uuid they are held by, which keeps the drift repair
       from revoking them while the table is being written;
    3. the `.lance-reserved` marker is written at the location;
    4. the ``__manifest`` row is recorded through RegisterTable's relative location. Measured on pylance 13.0.0, such a
       row answers ``is_only_declared`` until data lands, as the backend's own declaration does, and keeps no properties;
    5. ``after_register`` judges the row where it landed: a governed door refuses there a table recorded in a namespace
       other than the one its request was authorized on ([[LH-585]]). The backend records the row by name, so only a
       reading made once the row stands is one no drop and re-create of the namespace can follow: a namespace holding a
       row is not dropped without it.

    A failure after the claim removes the row when it was recorded, removes the marker and releases the claim; what
    ``before_register`` wrote is its caller's to take back. ``claims`` is ``None`` for a caller outside governance (a test building a fixture), which takes no claim.

    Raises:
        InvalidInputError: ``segments`` names a table outside every namespace: the `dir` backend resolves a root-level
            table by its ``<name>.lance`` directory, never by its row (measured on pylance 13.0.0).
        ConcurrentModificationError: Another table holds the location.
        TableAlreadyExistsError: A table already holds the name.
        PartiallyAppliedError: ``after_register`` refused the row and the backend kept it.
    """
    if len(segments) < 2:
        raise InvalidInputError(f"table {segments!r} names no namespace; a table the catalog makes lives in one")
    minted = table_uuid or str(uuid.uuid4())
    location = f"{root.rstrip('/')}/{minted}"
    if claims is not None:
        try:
            table_claims.claim_new_table(ns, claims, location, table=holder, segments=segments, table_uuid=minted)
        except location_claims.LocationHeldError:
            raise ConcurrentModificationError(f"the location minted for table {holder} is held by another table; retry to be given another") from None
    recorded = False
    try:
        if before_register is not None:
            before_register(minted, location)
        _reserve(location, so)
        native.call(ns, "register_table", RegisterTableRequest(id=segments, location=minted))
        recorded = True
        if after_register is not None:
            after_register()
    except Exception as exc:
        if recorded and not _row_removed(ns, segments):
            # The row stands, so its marker and claim stay with it: a table never stands at a location no claim holds.
            # A retry would meet it as already there, so the answer names it and its remedy.
            name = "$".join(segments)
            raise PartiallyAppliedError(
                f"the table {name} was recorded, its declaration was then refused ({exc}), and its row could not be removed",
                extensions={"table": name, "recorded": True, "remedy": "drop it before retrying"},
            ) from exc
        _unreserve(location, so, claims=claims, holder=holder)
        # The `dir` backend keeps namespaces and tables in one object-id space and arbitrates the add by
        # put-if-not-exists, so a namespace at the id surfaces as the add's conflict: a TableAlreadyExists on a local
        # root, a ConcurrentModification (14) on the e2e-stack lane's store (run 39029359505). Neither a retry nor a
        # table holds the id, so the answer is the already-exists code naming what does.
        if isinstance(exc, TableAlreadyExistsError | ConcurrentModificationError) and _namespace_holds(ns, segments):
            raise TableAlreadyExistsError(f"a namespace already holds the id {'$'.join(segments)}; a table cannot be created there") from exc
        raise
    return _described_location(ns, segments, fallback=location)


def _row_removed(ns: LanceNamespace, segments: list[str]) -> bool:
    """Take back the row a declaration recorded and is failing for; ``False``, logged, when the backend keeps it."""
    try:
        native.call(ns, "deregister_table", DeregisterTableRequest(id=segments))
    except Exception as exc:  # noqa: BLE001 — the declaration's own failure is what its caller is told
        log.error("declaration_row_not_removed", extra={"table": "$".join(segments), "error": str(exc)[:300]})
        return False
    return True


def stands_at(ns: LanceNamespace, segments: list[str], location: str) -> bool:
    """Whether a table stands under ``segments`` at ``location``: the backend describes the name there, declared or
    written. A describe that fails otherwise than with the name or its namespace absent answers that one may stand.

    For a door judging its own failed write, where :func:`table_claims.holder_stands` judges a claim: the write's
    rollback has released the claim, so there is no claim to judge, no trash keeps a table that was never written, and an
    unreadable answer is read on the side that keeps the table's grants.
    """
    try:
        found = existing_location(ns, segments)
    except NamespaceNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001 — an unreadable answer may be a table standing
        log.warning("table_standing_unread", extra={"table": "$".join(segments), "location": location, "error": str(exc)[:300]})
        return True
    return found is not None and decoded_path(found) == decoded_path(location)


def _namespace_holds(ns: LanceNamespace, segments: list[str]) -> bool:
    """Whether a namespace is at ``segments``; an unreadable answer is not one.

    The `dir` backend tells a namespace from a table of the same id: ``namespace_exists`` on a table's id raises
    ``NamespaceNotFoundError`` (measured on pylance 14.0.3, 2026-11-17).
    """
    try:
        ns.namespace_exists(NamespaceExistsRequest(id=segments))
    except Exception:  # noqa: BLE001 — only a namespace that answers present changes the conflict's wording
        return False
    return True


def _reserve(location: str, so: StorageOptions) -> None:
    """Write the `.lance-reserved` marker at ``location``, as :func:`delete_table_object` deletes: no directory object beside it."""
    if on_s3(location):
        bucket, prefix = split_s3_uri(location.rstrip("/"))
        object_client(so).put_object(Bucket=bucket, Key=f"{prefix}/{RESERVED_MARKER}", Body=b"")
        return
    fs, path = dataset_fs(location, so)
    fs.create_dir(path, recursive=True)
    with fs.open_output_stream(f"{path}/{RESERVED_MARKER}"):
        pass


def _unreserve(location: str, so: StorageOptions, *, claims: location_claims.ClaimStore | None, holder: str) -> None:
    """Undo a declaration that failed after its claim: the marker goes, then the claim. Logged, never raised over the failure."""
    try:
        delete_table_object(location, so, RESERVED_MARKER)
        if claims is not None:
            location_claims.release(claims, location, holder)
    except Exception as exc:  # noqa: BLE001 — the declaration's own failure is what the caller is told
        log.error("declared_location_not_released", extra={"table": holder, "location": location, "error": str(exc)[:300]})


def _described_location(ns: LanceNamespace, segments: list[str], *, fallback: str) -> str:
    """The location the backend describes ``segments`` at, spelled as every other door answers it; ``fallback`` when it does not answer."""
    try:
        described = native.call(ns, "describe_table", DescribeTableRequest(id=segments, check_declared=True))
    except BaseException as exc:  # noqa: BLE001 — the declaration committed; the caller keeps the location it was made at, a native panic too
        if not is_fault(exc):
            raise
        log.warning("declared_location_unresolved", extra={"table": segments, "error": str(exc)[:300]})
        return fallback
    return str(described.location or fallback)


def create_table(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    *,
    table: pa.Table,
    root: str,
    mode: str | CreateMode | None = None,
    properties: dict[str, str] | None = None,
    external_blob_bases: list[str] | None = None,
    data_bases: list[str] | None = None,
    registry: base_registry.BaseRegistry | None,
    holder: str = "",
    transaction_properties: dict[str, str] | None = None,
    recorded_author: str | None = None,
    overwriting: table_claims.DoorTable | None = None,
    before_commit: Callable[[str], None] | None = None,
    before_register: Callable[[str, str], None] | None = None,
    after_register: Callable[[], None] | None = None,
) -> CreateTableResponse:
    """Create a table at file format 2.2 with stable row ids — the ONLY create path (audit 2026-07-14).

    Serves EVERY create, and IS the door: this used to sit behind a same-named 36-line wrapper that
    forwarded all nine arguments and renamed two on the way (CAT-CORE-17). 2.2 + stable row ids are
    create-time-only, so a table that skipped this path could never gain the durable row identity
    ``row_id_lineage`` needs (row-level provenance: model → dataset version → the exact source rows).

    ``table`` arrives decoded: the door reads the body with :func:`read_arrow_body` before its
    idempotency claim, so a body that is not an Arrow stream is refused 400 with nothing written.

    Runs off the event loop (blocking Lance/S3 IO), so the endpoint stays a single delegated call.
    ``external_blob_bases`` are the external bases the table registers: ``Blob.from_uri`` pointers are
    accepted only under one of them.
    ``data_bases`` (#3-B) spreads the table's fragments across N approved buckets (Lance multi-base);
    empty (the default) → a single-base write.

    Honours the create ``mode`` against a *written* table (``ExistOk`` keeps it, ``Overwrite`` writes a new
    version of it that keeps the table's provenance — :func:`provenance_guard.keep_provenance` — while its
    history stays readable, ``Create`` conflicts). A *declared-only* table — one that exists in the namespace but was never
    written (a bare ``POST /declare``, or a declare→write that crashed before the write) — has no readable
    dataset, so every mode simply lands the first data version into its already-declared location; this keeps
    the multi-step create idempotent and crash-safe, and never opens a table that isn't there (which 500'd).
    A brand-new table is declared at ``<root>/<table uuid>`` (:func:`declare_table`, ADR 0173 §5), the namespace
    connection's ``root``, then written at ``data_storage_version="2.2"`` (lance_docs/lance/guide/blob.md — Version
    Compatibility). Any failed fresh write is rolled back with ``drop_table`` so the name stays retryable rather than
    stuck describable-but-unreadable.

    ``properties`` are stamped on the table's schema metadata in the data's commit ([[LH-503]]) and kept nowhere else:
    the declaration is a RegisterTable, which keeps none, as the spec allows (`lance_docs/ns_catalog/spec.yaml:2719-2724`),
    so the response names none either.

    ``registry`` is where a fresh write records the bases it registers ([[LH-279]]): the record is the
    catalog's word on which foreign bases the table may declare, written BEFORE the manifest and removed
    again when the write fails. Required and never defaulted — the governed door passes the control root;
    ``None`` is an explicit choice for a caller outside governance (a test building a fixture), which
    leaves the table with no record at all. The table's location claim, which carries its uuid ([[LH-519]],
    ADR 0173 §6), lives beside the record: a table this call declares is given the uuid its location is named
    after, and one an earlier request declared keeps the uuid its declaration's claim records. ``holder`` is the
    table's canonical id, required whenever ``registry`` is given.

    ``transaction_properties`` ride the commit that writes the data (the run a governed create names).
    ``recorded_author`` is the caller the door authorized the write for, recorded as its author once it lands (ADR 0187);
    ``None`` records nobody: a write the emitter leaves unannounced, or one the catalog authenticated no caller for.

    ``overwriting`` is the table an ``Overwrite`` asked ``can_drop`` on, as its door resolved it ([[LH-530]]): the write
    lands at that location once the name still resolves there.

    ``before_commit`` is handed the uuid of the table about to be written, once its location's claim holds it and before
    the data's commit: a governed create records there what the commit declares it read ([[LH-541]]). A failure in it
    fails the create as a failed write does.

    ``before_register`` is handed the uuid and location of a table this call brings into being, by :func:`declare_table`, before the
    backend records it: a governed create writes the table's grants there ([[LH-588]]). It is not called for a table the
    call finds under the name (written, or declared by an earlier request), which holds its own grants.

    Raises:
        ConcurrentModificationError: The table an ``Overwrite`` resolved changed since.
    """
    if registry is not None and not holder:
        raise ValueError("a governed create names the table that holds its location and data directories")
    external_blob_bases = external_blob_bases or []
    normalized = CreateMode.parse(mode)
    existing = overwriting.location if overwriting is not None else existing_location(ns, segments)
    only_declared = existing is not None and not holds_a_version(existing, so)

    if existing is not None and not only_declared:  # a written, readable table already lives here
        if normalized is CreateMode.OVERWRITE:
            # A new version of the same table, so the table's provenance rides onto it ([[LH-242]]).
            # Disarmed first ([[LH-245]]): an overwrite carries the table's config forward, keys included.
            if overwriting is not None:
                # Lance commits the disarm and the overwrite itself, so both wait out another commit's turn here ([[LH-259]]).
                overwriting.wait_for_turn(None)
            opened = (
                overwriting.open_unchecked(so)
                if overwriting is not None
                else open_dataset(existing, storage_options=so.in_clear(), session=shared_lance_session())
            )
            current = disarm_commit_path_cleanup(opened, segments)
            # The table's key, not the payload's: a key is fixed once set (`lance_docs/lance/format/table/index.md:87`).
            primary_keys.refuse_repeats_within(table, primary_keys.key_columns(current.lance_schema), door="create")
            kept = provenance_guard.keep_provenance(current.schema, table)
            if overwriting is not None:
                # An Overwrite is a new version of the same table, so it keeps what the table's contract names (ADR 0212).
                contracts.require_kept(overwriting.contract(), current.schema, kept.schema, door="create")
            _before_commit(before_commit, existing, registry)
            dataset = _write_blob(
                kept,
                existing,
                so,
                mode="overwrite",
                external_blob_bases=external_blob_bases,
                data_bases=registered_data_bases(data_bases, current) if data_bases else None,
                properties=properties,
                transaction_properties=transaction_properties,
            )
            _record_written(registry, existing, so, written=dataset, author=recorded_author)
            return CreateTableResponse(location=existing, version=dataset.version)
        if normalized is CreateMode.EXIST_OK:  # keep it untouched, just report its current version
            return _kept_at(existing, so)
        # A written table under `Create` is the create's conflict, whatever the payload holds: the spec's 409 comes
        # before a judgement of rows that will never be written ([[LH-424]]).
        raise TableAlreadyExistsError(f"Table already exists: {'$'.join(segments)}")

    # BEFORE the declare, so a refused create leaves no name behind ([[LH-243]]).
    primary_keys.refuse_repeats_within(table, primary_keys.declared_key(table.schema), door="create")
    if existing is not None and only_declared:  # declared, no data yet → write into it (all modes)
        if overwriting is not None:
            overwriting.require_held()
        if registry is not None:
            table_claims.take(ns, _claim_store(registry), existing, table=holder, segments=segments)
        return _write_blob_into(
            ns,
            _named_by_its_uuid(table, existing, registry),
            location=existing,
            so=so,
            segments=segments,
            properties=properties,
            external_blob_bases=external_blob_bases,
            data_bases=data_bases,
            registry=registry,
            holder=holder,
            transaction_properties=transaction_properties,
            recorded_author=recorded_author,
            before_commit=before_commit,
        )

    location = declare_table(
        ns,
        so,
        segments,
        root=root,
        claims=None if registry is None else _claim_store(registry),
        holder=holder,
        before_register=before_register,
        after_register=after_register,
    )
    return _write_blob_into(
        ns,
        _named_by_its_uuid(table, location, registry),
        location=location,
        so=so,
        segments=segments,
        properties=properties,
        external_blob_bases=external_blob_bases,
        data_bases=data_bases,
        registry=registry,
        holder=holder,
        transaction_properties=transaction_properties,
        recorded_author=recorded_author,
        before_commit=before_commit,
    )


def _before_commit(hook: Callable[[str], None] | None, location: str, registry: base_registry.BaseRegistry | None) -> None:
    """Hand ``hook`` the uuid the claim at ``location`` records; nothing for a caller outside governance, whose table holds
    no claim."""
    if hook is None or registry is None:
        return
    hook(_claimed_uuid(registry, location))


def _claimed_uuid(registry: base_registry.BaseRegistry, location: str) -> str:
    """The uuid the claim at ``location`` records: the table a governed create is about to write there.

    Raises:
        InternalError: The location holds no claim recording a uuid, which a governed create always takes first.
    """
    claim = location_claims.read(_claim_store(registry), location)
    if claim is None or claim.claim.uuid is None:
        raise InternalError(f"the table about to be written at {location} holds no claim recording its uuid")
    return claim.claim.uuid


def _record_written(registry: base_registry.BaseRegistry | None, location: str, so: StorageOptions, *, written: lance.LanceDataset, author: str | None) -> None:
    """Record what the create door's write added ([[LH-419]]) and the author it was made for (ADR 0187), under the uuid the
    location's claim records; nothing for a caller outside governance. ``write_dataset`` writes and commits in one call, so
    both are recorded once it lands (:func:`committed_files.record_landed`, :func:`commit_authors.record_landed`)."""
    if registry is None:
        return
    uuids: list[str] = []

    def table_uuid() -> str:
        if not uuids:
            claim = location_claims.read(_claim_store(registry), location)
            if claim is None or claim.claim.uuid is None:
                raise InternalError(f"the table written at {location} holds no claim recording its uuid")
            uuids.append(claim.claim.uuid)
        return uuids[0]

    def record_files(committed: file_identities.CommittedFiles) -> None:
        file_identities.record(registry.control_root, registry.storage_options, table_uuid(), committed=committed)

    def record_author(transaction_uuid: str, version: int) -> None:
        if author is not None:
            commit_records.record_author(
                registry.control_root,
                registry.storage_options,
                table_uuid(),
                transaction_uuid=transaction_uuid,
                version=version,
                ref="",
                author=author,
                manifest=None,
            )

    transaction = committed_files.record_landed(record_files, written, table_location=location, branch=None, so=so)
    if author is not None:
        commit_authors.record_landed(record_author, transaction, version=int(written.version), table_location=location)


def _named_by_its_uuid(table: pa.Table, location: str, registry: base_registry.BaseRegistry | None) -> pa.Table:
    """``table`` with ``lineage.dataset_id`` naming the uuid its location's claim records (ADR 0173 §10), for a table this
    create brings into being; an Overwrite keeps the table's own (:func:`provenance_guard.keep_provenance`).

    The uuid, not the name: a name can be given up and taken by another table, so a file stamped with one names whichever
    table holds it today. A governed create claims the location before writing, so the claim is there to read; ``None``
    for ``registry``, a caller outside governance, stamps nothing.
    """
    if registry is None:
        return table
    claim = location_claims.read(_claim_store(registry), location)
    return declare_dataset_id(table, claim.claim.uuid if claim is not None else None)


def _claim_store(registry: base_registry.BaseRegistry) -> location_claims.ClaimStore:
    """The location claims beside ``registry``'s base records, on the same control root and credential."""
    return location_claims.ClaimStore(control_root=registry.control_root, storage_options=registry.storage_options)


def _write_blob_into(
    ns: LanceNamespace,
    table: pa.Table,
    *,
    location: str,
    so: StorageOptions,
    segments: list[str],
    properties: dict[str, str] | None,
    external_blob_bases: list[str],
    data_bases: list[str] | None = None,
    registry: base_registry.BaseRegistry | None,
    holder: str = "",
    transaction_properties: dict[str, str] | None = None,
    recorded_author: str | None = None,
    before_commit: Callable[[str], None] | None = None,
) -> CreateTableResponse:
    """Write the blob table's first data version into an already-declared ``location``, rolling the declare
    back with ``drop_table`` on failure so the name stays retryable rather than stuck declared-but-unreadable.

    THE BASE RECORD IS CLAIMED FIRST ([[LH-279]]): every base this write registers through
    ``initial_bases`` — the external blob base the create asked for and the approved data bases — is in the
    catalog's record before the manifest that declares them exists, so no reader ever meets the table
    declaring a base its record lacks. A failed write releases exactly what it claimed; a record that
    outlived its write would vouch for a base no manifest names.

    EACH DATA DIRECTORY IS CLAIMED FOR THE TABLE FIRST OF ALL ([[LH-252]]), put-if-not-exists, under the uuid its
    location's claim records ([[LH-575]]), so no other table — a registration declaring the same directory, under this
    name or any other — can be admitted to it while this one exists.

    The location's own claim is :func:`create_table`'s, taken before this write ([[LH-519]]); a failed write
    releases it with the table it drops.
    """
    claim: base_registry.BaseClaim | None = None
    directories: list[str] = []
    claimed_uuid: str | None = None
    try:
        # ONE DIRECTORY PER TABLE under each approved base ([[LH-252]]), minted here because the record
        # below and the manifest must name the same path.
        own_bases = table_data_bases(data_bases or [], uuid.uuid4().hex)
        if registry is not None:
            entries = table_bases.create_entries(external_blob_bases, [(base.path, base.name) for base in own_bases])
            claimed_uuid = _claimed_uuid(registry, location)
            claimant = location_claims.DirectoryClaimant(uuid=claimed_uuid, root=location, table=holder, segments=segments)
            directories = base_registry.claim_data_directories(registry, entries, claimant)
            claim = base_registry.claim_bases(registry, location, entries)
        # STAMPED ON THE TABLE, not only on the manifest row and the reply. The spec's "properties at create" was true of
        # `declare_table` and of the response echo and false of the Lance file, so a client that created with
        # `{"owner": …}` and then asked the TABLE got nothing back and no error saying why. Written as the table's schema
        # metadata IN THE DATA'S COMMIT, the one that carries the writer's run and author ([[LH-503]]): a commit of
        # their own after it would be a version naming neither. MERGED over the payload's own metadata, the property
        # winning, as `update_schema_metadata` merges.
        if properties:
            table = table.replace_schema_metadata({**(table.schema.metadata or {}), **{key.encode(): value.encode() for key, value in properties.items()}})
        _before_commit(before_commit, location, registry)
        dataset = _write_blob(
            table,
            location,
            so,
            mode="create",
            external_blob_bases=external_blob_bases,
            data_bases=own_bases,
            properties=properties,
            transaction_properties=transaction_properties,
        )
    except BaseException as exc:
        # A Rust PANIC is a pyo3 `PanicException`, which derives from BaseException, so `except Exception` would skip
        # the rollback and leave the name declared with no dataset behind it ([[LH-302]]).
        # A failed drop leaves the name declared with no dataset behind it: logged as the release below is, while the write
        # error stays what the caller is told. A name nothing is declared under has nothing to roll back.
        _drop_the_declared_name(ns, segments, location=location)
        if registry is not None:
            _release_what_the_write_took(registry, location=location, claim=claim, directories=directories, claimed_uuid=claimed_uuid, holder=holder)
        if isinstance(exc, Exception | KeyboardInterrupt | SystemExit | GeneratorExit):
            if isinstance(exc, OSError) and _UNSUPPORTED_TYPE_MARKER in str(exc):
                raise InvalidInputError(f"the table's schema holds a type Lance does not store: {clean_lance_message(str(exc))}") from exc
            raise
        raise InternalError("the table's first write panicked in Lance; the create was rolled back and may be retried") from exc
    _record_written(registry, location, so, written=dataset, author=recorded_author)
    return CreateTableResponse(location=location, version=dataset.version)


def _drop_the_declared_name(ns: LanceNamespace, segments: list[str], *, location: str) -> None:
    """Drop the name a failed first write declared, logging a drop that fails."""
    try:
        with suppress(TableNotFoundError):
            ns.drop_table(DropTableRequest(id=segments))
    except Exception as drop_exc:  # noqa: BLE001 — the write error is what the caller is told
        log.error("create_rollback_drop_failed", extra={"table": "$".join(segments), "location": location, "error": str(drop_exc)[:300]})


def _release_what_the_write_took(
    registry: base_registry.BaseRegistry,
    *,
    location: str,
    claim: base_registry.BaseClaim | None,
    directories: list[str],
    claimed_uuid: str | None,
    holder: str,
) -> None:
    """Release the base record, data directories and location claim a failed first write took, logging a release that fails."""
    try:
        if claim is not None:
            base_registry.release_claim(registry, claim)
        if claimed_uuid is not None:
            base_registry.release_data_directories(registry, directories, uuid=claimed_uuid)
        location_claims.release(_claim_store(registry), location, holder, uuid=claimed_uuid)
    except Exception as release_exc:  # noqa: BLE001 — the write error is what the caller is told
        log.error("create_base_record_release_failed", extra={"location": location, "error": str(release_exc)[:300]})


#: Lance's refusal of an Arrow type it does not store, as a bare `OSError`: "LanceError(Schema): Unsupported data type:
#: ListView(...)" for list_view, large_list_view, run-end encoded, union, month_day_nano and decimal32 columns (measured
#: on pylance 13.0.0). The caller's schema, so InvalidInput (13) rather than Internal ([[LH-301]]).
_UNSUPPORTED_TYPE_MARKER = "Unsupported data type:"


def kept_table(ns: LanceNamespace, so: StorageOptions, segments: list[str]) -> CreateTableResponse | None:
    """What an ExistOk create keeps at ``segments``: the written table's location and version, or ``None`` while no
    table there holds a version (none at all, or one only declared)."""
    existing = existing_location(ns, segments)
    if existing is None or not holds_a_version(existing, so):
        return None
    return _kept_at(existing, so)


def _kept_at(location: str, so: StorageOptions) -> CreateTableResponse:
    return CreateTableResponse(location=location, version=open_dataset(location, storage_options=so.in_clear(), session=shared_lance_session()).version)


def existing_location(ns: LanceNamespace, segments: list[str]) -> str | None:
    """The location the table is described at, or ``None`` if no table has the name; a declared table has one too.

    Measured on pylance 13.0.0, the `dir` backend describes a declared table without ``check_declared``. Whether the
    table holds a version is :func:`holds_a_version`'s to judge, never the backend's verdict ([[LH-528]]): a declared
    table has no readable dataset, so a caller must not open it.
    """
    try:
        resp = ns.describe_table(DescribeTableRequest(id=segments, with_table_uri=True))
    except TableNotFoundError:
        return None
    return getattr(resp, "table_uri", None) or getattr(resp, "location", None)
