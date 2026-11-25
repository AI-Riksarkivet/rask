"""A destructive drop: the table's bytes deleted at the location its door resolved, refused while a registered clone
reads them or while the location is not one table's bytes."""

import logging
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from typing import Final

import pyarrow.fs as pafs
from lance_namespace import (
    DeregisterTableRequest,
    DeregisterTableResponse,
    DescribeTableRequest,
    DropTableResponse,
    InternalError,
    InvalidTableStateError,
    LanceNamespace,
    ServiceUnavailableError,
    TableNotFoundError,
)

from catalog.core import native
from catalog.core.table_objects import RESERVED_MARKER, dataset_fs
from lakehouse.lancekit.base_refs import decoded_path, decoded_segments, location_in_store, normalise, store_of
from lakehouse.naming import CATALOG_DELIMITER
from lakehouse.problems.ns_errors import PartiallyAppliedError
from lakehouse.records import base_registry, clone_sources, data_directories, location_claims, trash
from lakehouse.records.control_prefixes import CONTROL_PREFIXES
from lakehouse.records.trash import TrashRecord
from lakehouse.storage import stored_objects
from lakehouse.storage.options import StorageOptions, on_s3


__all__ = [
    "TableBytesLeftError",
    "clone_readers",
    "destroy_table",
    "require_no_clone_reading",
]


log = logging.getLogger(__name__)


#: The Lance table-layout directory a dataset holds; a location holding objects and none under it is no dataset.
_VERSIONS_DIR: Final = "_versions"


#: How many list-and-delete passes a destroy makes before it reports what is left. A pass deletes every object it
#: listed, so a second pass finds only what a writer put meanwhile.
_DELETE_PASSES: Final = 3


class TableBytesLeftError(PartiallyAppliedError):
    """A destructive drop removed the table from ``__manifest`` and could not delete every object at its location."""


def destroy_table(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    *,
    location: str,
    estate_roots: Sequence[str],
    table_uuid: str,
    namespace_uuid: str | None,
    control: location_claims.ClaimStore,
    dropped_by: str | None = None,
    before_removal: Callable[[], None] | None = None,
) -> DropTableResponse:
    """Destroy the table ``segments`` names, whose location its door resolved to ``location`` ([[LH-532]]).

    DropTable deletes the table's data (``lance_docs/ns_catalog/spec.yaml:498``), and the data is where Lance opens it:
    at the key the location decodes to once (:func:`dataset_fs`), with the `dir` catalog's reservation marker under the
    location as spelled (``maintenance.services.purge.store_paths`` measures the split). The backend's own drop deletes
    ``base_path.join(location)``, which percent-encodes the location as one segment, so for a table registered at
    ``outside/held`` or ``räksmörgås`` it removes the row and deletes nothing (on a local root it raises after the row
    is gone; ``rust/lance-namespace-impls/src/dir/manifest.rs:3180-3211`` at lance v13.0.0). So the catalog keeps the
    backend's order, the row first and then the data: it judges the bytes, removes the row with DeregisterTable, which
    deletes nothing, and deletes the bytes itself with ``so``, its own session:

    1. Judged before anything changes: the location must name one place below a store root, outside every control
       prefix, and neither be nor contain an ``estate_roots`` entry; at its key it must hold nothing, a ``_versions/``
       tree, or the reservation marker alone (a declared table); as spelled, at most the marker. A refusal leaves the
       table registered and every byte in place. The table's own data directories on its data bases are read from its
       base record here too ([[LH-286]], :func:`lakehouse.records.data_directories.directories_to_end`), since
       they are its bytes as much as its root is.
    2. A location holding a ``_versions/`` tree gets a residue trash record on ``control`` before the row goes
       (:func:`trash.make_record`'s ``residue``, grace 0), so bytes a failed delete leaves keep their location's claim
       (``table_claims.kept_in_trash``): no register adopts them, undrop refuses them, the purge reclaims them. A
       location holding at most the marker gets none and its claim lapses as usual: nothing opens a marker alone, so a
       register attaching it later adopts no data, and the purge refuses a location with no ``_versions/``, so a
       record there would hold the claim forever (both measured on pylance 13.0.0 over moto).
    3. ``before_removal``, when given, runs once every judgement has passed and the residue record is filed, right
       before the row goes; if it raises, the residue record is cleared and the error raised: nothing changed.
       The row goes: the commit point. A deregister that answers TableNotFound found no row, and that answer is
       raised unchanged. Any other failure is judged by whether the table is still described: if it is, nothing was
       removed and the failure is raised; if it is not, the row went and its answer was lost, so the drop goes on.
    4. Every object under both paths is deleted, absent counting as deleted, until a listing finds none, with
       ``_versions/`` last, so a delete that stops part-way leaves the marker the purge recognises a dataset by. On S3
       the delete is DeleteObjects on the listed keys, which writes no directory marker (pyarrow's delete PUTs one,
       :func:`delete_table_object`). Then every object in the table's data directories, by key; the caller forgets the
       base record that names them once this answers.
    5. The residue record is cleared, and the drop answers the row it removed, or the judged location when the
       deregister's answer was lost.

    Raises:
        InvalidTableStateError: The location is not one a drop may delete, or a registered clone reads through it
            (:func:`require_no_clone_reading`); nothing changed.
        ServiceUnavailableError: The table's base record, a data directory's claim or its tags could not be read; nothing
            changed.
        TableNotFoundError: The backend holds no such table; nothing changed.
        TableBytesLeftError: The row is gone and objects remain at the location or in a data directory, or the row
            removed named another location, whose bytes are left alone. The residue record stands.
    """
    stored, spelled = _table_paths(location)
    holds_a_dataset = _require_destroyable(location, so, stored, spelled=spelled, estate_roots=estate_roots)
    table = CATALOG_DELIMITER.join(segments)
    require_no_clone_reading(location, so, table=table)
    directories = _data_directories_of(control, location, table, uuid=table_uuid)
    residue = (
        trash.make_record(table, uuid=table_uuid, location=location, dropped_by=dropped_by, grace_days=0, namespace_uuid=namespace_uuid, residue=True)
        if holds_a_dataset
        else None
    )
    if residue is not None:
        trash.put(control.control_root, control.storage_options, residue)
    if before_removal is not None:
        # The caller's last step before the row goes, once every judgement has passed: a door files what the row's
        # removal leaves it owing here (a clone's ending), so a refusal above files nothing. A failure changed nothing.
        try:
            before_removal()
        except Exception:
            _clear_residue(control, residue)
            raise
    removed = _deregister_row(ns, segments, control=control, residue=residue, table=table, location=location)
    try:
        if removed is not None and decoded_path(str(removed.location or "")) != decoded_path(location):
            raise InternalError(f"the row removed for {table} named {removed.location!r}, not the location judged")
        _delete_everything_under(location, so, stored, spelled=spelled)
        data_directories.delete_directories(directories, so)
    except Exception as exc:
        raise _bytes_left(table, location, exc, held=residue is not None) from exc
    _clear_residue(control, residue)
    if removed is None:
        return DropTableResponse(id=segments, location=location)
    return DropTableResponse(id=removed.id, location=removed.location)


def _deregister_row(
    ns: LanceNamespace, segments: list[str], *, control: location_claims.ClaimStore, residue: TrashRecord | None, table: str, location: str
) -> DeregisterTableResponse | None:
    """Remove the table's row: the row removed, ``None`` when its answer was lost, the residue cleared when the row stands."""
    try:
        return native.call(ns, "deregister_table", DeregisterTableRequest(id=segments))
    except TableNotFoundError:
        _clear_residue(control, residue)
        raise
    except Exception as exc:
        if _still_described(ns, segments):
            _clear_residue(control, residue)
            raise
        log.warning("drop_deregister_answer_lost", extra={"table": table, "location": location, "error": str(exc)[:300]})
        return None


def _data_directories_of(control: location_claims.ClaimStore, location: str, table: str, *, uuid: str) -> list[base_registry.RecordedBase]:
    """The data directories a drop of ``table`` (the table ``uuid``) at ``location`` deletes with its root, read before
    anything changes.

    Raises:
        ServiceUnavailableError: The base record or a directory's claim could not be read.
    """
    registry = base_registry.BaseRegistry(control_root=control.control_root, storage_options=control.storage_options)
    try:
        return data_directories.directories_to_end(registry, location, uuid=uuid)
    except (base_registry.UnreadableBaseRecordError, OSError, ValueError) as exc:
        log.error("drop_data_directories_unread", extra={"table": table, "location": location, "error": str(exc)[:300]})
        raise ServiceUnavailableError(f"the data directories of {table} could not be read, so nothing was dropped") from exc


def _clear_residue(control: location_claims.ClaimStore, residue: TrashRecord | None) -> None:
    """Clear the residue record a drop filed, once it no longer stands for bytes left. Never raises.

    A record that outlives its drop holds nothing up: it names a location that is empty or that a live table still
    holds, the purge refuses the second (still registered) and finishes the first as a no-op, and the next drop of the
    table overwrites it.
    """
    if residue is None:
        return
    try:
        trash.clear(control.control_root, control.storage_options, str(residue.uuid))
    except Exception as exc:  # noqa: BLE001 — the drop's outcome is decided; a stale record is logged, not raised
        log.error("drop_residue_record_not_cleared", extra={"table": residue.id, "location": residue.location, "error": str(exc)[:300]})


def _still_described(ns: LanceNamespace, segments: list[str]) -> bool:
    """Whether the backend still describes ``segments``; a describe that fails otherwise answers yes, since the row's fate is unknown."""
    try:
        native.call(ns, "describe_table", DescribeTableRequest(id=segments))
    except TableNotFoundError:
        return False
    except Exception:  # noqa: BLE001 — an unreadable table is one whose row may stand
        return True
    return True


def _bytes_left(table: str, location: str, cause: Exception, *, held: bool) -> TableBytesLeftError:
    """The answer for a drop whose row is gone while objects remain at its location, logged at ERROR with the location.

    ``held`` says a residue trash record keeps the location's claim for the objects; without one the location held at
    most the reservation marker, which nothing opens.
    """
    log.error("table_bytes_left_after_drop", extra={"table": table, "location": location, "error": str(cause)[:300], "residue_record": held})
    remedy = (
        "the table no longer resolves; a residue trash record keeps its location held, and the trash purge reclaims the objects"
        if held
        else "the table no longer resolves; its location held only the reservation marker, which no table opens, and its claim lapses"
    )
    return TableBytesLeftError(
        f"table {table} was removed from the catalog and objects remain at {location}",
        extensions={"table": table, "location": location, "removed_from_catalog": True, "bytes_left": True, "remedy": remedy},
    )


def _table_paths(location: str) -> tuple[str, str]:
    """The two paths a table recorded at ``location`` keeps objects under: its decoded key and its spelling.

    Each is a store path: ``bucket/key`` on S3, an absolute path on a local root (a ``file://`` location's path).
    """
    anchor = "/" if store_of(location) == "file" else ""
    return f"{anchor}{decoded_path(location)}", f"{anchor}{normalise(location)}"


def _objects_under(location: str, so: StorageOptions, path: str) -> list[str]:
    """Every object under ``path/``, as a store path; on S3 a directory marker is one.

    Raises:
        OSError: The store refused the listing, or did not answer it.
    """
    if on_s3(location):
        return [stored.path for stored in stored_objects.iter_under(f"s3://{path}", so)]
    fs, _ = dataset_fs(location, so)
    listed = fs.get_file_info(pafs.FileSelector(path, recursive=True, allow_not_found=True))
    return [info.path for info in listed if info.type == pafs.FileType.File]


def _contents(path: str, objects: Iterable[str]) -> list[str]:
    """``objects`` named relative to ``path``, without directory markers, which hold nothing."""
    prefix = f"{path.rstrip('/')}/"
    return sorted(name for name in (obj.removeprefix(prefix) for obj in objects) if name and not name.endswith("/"))


def clone_readers(location: str, so: StorageOptions, *, table: str) -> set[str]:
    """The tags of the registered clones that read the table at ``location`` (:func:`clone_sources.clones_reading`).

    Raises:
        ServiceUnavailableError: Its tags could not be listed, so whether a clone reads it is unknown.
    """
    try:
        return set(clone_sources.clones_reading(location, so))
    except OSError as exc:
        raise ServiceUnavailableError(f"the tags of table {table} could not be listed, so whether a clone reads it is unknown; nothing changed") from exc


def require_no_clone_reading(location: str, so: StorageOptions, *, table: str) -> None:
    """Refuse destroying the bytes at ``location`` while a registered clone reads through them (ADR 0198 §1).

    A clone reads its source's data files where they are (`lance_docs/lance/format/table/blob.md` § Clones), so the
    source's bytes are the clone's too; the tag each clone holds on its source says so (:mod:`lakehouse.records.clone_sources`).
    A recoverable drop keeps the bytes and is not refused; its purge asks this again.

    Raises:
        InvalidTableStateError: The table carries a registered clone's tag (code 19, 409).
        ServiceUnavailableError: Its tags could not be listed, so whether a clone reads it is unknown.
    """
    if readers := clone_readers(location, so, table=table):
        log.warning("destroy_refused_clone_source", extra={"table": table, "location": location, "clones": readers})
        raise InvalidTableStateError(
            f"table {table} cannot be destroyed while {len(readers)} registered clone(s) read its files: drop or deregister the clones first, "
            "or drop it recoverably"
        )


def _require_destroyable(location: str, so: StorageOptions, stored: str, *, spelled: str, estate_roots: Sequence[str]) -> bool:
    """Refuse a drop whose location is not one table's bytes, before anything changes (:func:`destroy_table`, step 1).

    Answers whether the location holds a ``_versions/`` tree: a dataset, rather than nothing or a declaration's marker.

    Raises:
        InvalidTableStateError: The location names no single place, a store root, a control prefix or an estate root
            (or a place holding one), or it holds objects that are not a Lance table's.
    """
    segments = decoded_segments(location)
    if segments is None or (store_of(location) != "file" and len(segments) < 2):
        raise InvalidTableStateError(f"the table's location {location!r} names no single place below a store root, so the drop deletes nothing")
    if crossed := sorted(CONTROL_PREFIXES.intersection(segments)):
        raise InvalidTableStateError(f"the table's location {location!r} crosses the control prefix(es) {crossed}, so the drop deletes nothing")
    if any(root and location_in_store(location, root) for root in estate_roots):
        raise InvalidTableStateError(f"the table's location {location!r} holds an estate root, so the drop deletes nothing")
    held = _contents(stored, _objects_under(location, so, stored))
    holds_versions = any(name.split("/")[0] == _VERSIONS_DIR for name in held)
    if held and held != [RESERVED_MARKER] and not holds_versions:
        log.warning("drop_refused_not_a_dataset", extra={"location": location, "objects": held[:5], "count": len(held)})
        raise InvalidTableStateError(
            f"the table's location {location!r} holds {len(held)} object(s) and no {_VERSIONS_DIR}/, so it is not a Lance table and the drop deletes nothing"
        )
    if spelled != stored:
        stray = [name for name in _contents(spelled, _objects_under(location, so, spelled)) if name != RESERVED_MARKER]
        if stray:
            log.warning("drop_refused_foreign_objects", extra={"location": location, "objects": stray[:5], "count": len(stray)})
            raise InvalidTableStateError(
                f"the table's location {location!r}, as spelled, holds {len(stray)} object(s) beside its reservation marker, so the drop deletes nothing"
            )
    return holds_versions


def _delete_everything_under(location: str, so: StorageOptions, stored: str, *, spelled: str) -> None:
    """Delete every object under ``stored`` and ``spelled``, ``_versions/`` last, then prove none is left (:func:`destroy_table`, step 4).

    The table's ``_versions/`` tree goes after every other object, so a delete that stops part-way leaves a location
    the purge still recognises as a dataset (``maintenance.services.purge._require_a_dataset``) and can finish.

    Raises:
        OSError: An object remains after every pass, or the store refused a delete.
    """
    paths = sorted({stored, spelled})
    versions = f"{stored.rstrip('/')}/{_VERSIONS_DIR}/"
    for _ in range(_DELETE_PASSES):
        held = [obj for path in paths for obj in _objects_under(location, so, path)]
        if not held:
            return
        stored_objects.delete_objects(location, so, [obj for obj in held if not obj.startswith(versions)])
        stored_objects.delete_objects(location, so, [obj for obj in held if obj.startswith(versions)])
        if not on_s3(location):
            _delete_local_directories(location, so, [versions.rstrip("/"), *paths])
    left = [obj for path in paths for obj in _objects_under(location, so, path)]
    if left:
        raise OSError(f"{len(left)} object(s) remain under {location} after {_DELETE_PASSES} delete passes: {left[:5]}")


def _delete_local_directories(location: str, so: StorageOptions, paths: list[str]) -> None:
    """Delete each of ``paths`` as a directory of the filesystem ``location`` is on, one already gone counting as deleted."""
    fs, _ = dataset_fs(location, so)
    for path in paths:
        with suppress(FileNotFoundError):
            fs.delete_dir(path)
