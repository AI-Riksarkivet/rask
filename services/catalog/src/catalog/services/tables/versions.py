"""What a committed version says: the version, schema facet, location and time a write's announcement reads back, the
version a transaction committed, a branch's identifier, a tag's ref, and the table's commit log."""

from __future__ import annotations

import logging
from typing import Any, Final, cast

import lance
import pyarrow as pa
from lance_namespace import DescribeTransactionRequest, ServiceUnavailableError
from pydantic import BaseModel

from catalog.core import manifest_bytes
from catalog.core.namespace import open_resolved_dataset, open_resolved_dataset_unchecked
from catalog.core.schema_facet import SchemaFields, facet_fields
from catalog.core.version_retention import recorded_branch
from catalog.services import table_claims
from lakehouse.lancekit.transactions import read_transaction
from lakehouse.lancekit.versions import committed_at
from lakehouse.problems.outage import reads_as_store_outage
from lakehouse.process.supervision import is_fault
from lakehouse.records import commit_records
from lakehouse.storage.options import StorageOptions


__all__ = [
    "BRANCH_IDENTIFIER_KEY",
    "CommitReadback",
    "branch_identifier",
    "commit_ref_key",
    "commit_time",
    "committed_version",
    "identifier_of",
    "payload_schema_fields",
    "read_version_and_schema",
    "table_history",
    "tag_event_ref",
]


log = logging.getLogger(__name__)


class CommitReadback(BaseModel):
    """What a committed write's announcement reads back of the commit it made."""

    #: The version the write committed, ``None`` when it could not be identified.
    version: int | None
    #: The per-version column schema, empty when it could not be read.
    schema_fields: SchemaFields
    #: The dataset's physical URI, the standard ``dataSource`` facet.
    uri: str | None
    #: When the version was committed (the manifest's own time), ISO 8601 in UTC: the announcement's ``eventTime``, so
    #: the announcement and a re-announcement of the same commit carry the same instant ([[LH-497]]).
    committed_at: str | None = None


def read_version_and_schema(
    location: str,
    so: StorageOptions,
    table_id: list[str],
    *,
    pin_version: int | None,
    branch: str | None,
) -> CommitReadback:
    """The version, schema-facet fields, location and commit time for stamping lineage after a committed write.

    ``pin_version`` is the version the write COMMITTED, as the write itself reported it: a handle's
    ``version`` after the commit, a response's ``version``, or :func:`committed_version` for a response
    carrying only a ``transaction_id``. ONE dataset open, AT that version, serves the schema and the
    location, so neither can come from another writer's snapshot. ``None`` means the commit could not be
    identified, and the answer is then versionless and schemaless: the latest snapshot names whichever
    commit landed last, which under concurrent appends is somebody else's (measured on pylance 12.0.0:
    two handles appending in turn commit v3 and v4, and a reopen after both reports v4 for each).

    ``location`` is where the write committed, the location its door resolved ([[LH-530]]): a name read again after
    the commit may name another table by then. ``branch`` must name the ref the write COMMITTED TO: a branch keeps its
    own version sequence, so version N opened on main is a different snapshot.

    Entirely best-effort: the write is already committed, so a readback failure must degrade the lineage
    enrichment (the pinned version alone), never fail the request.
    """
    try:
        dataset = open_resolved_dataset(location, so, table_id, version=pin_version, branch=branch)
        # The physical URI rides the SAME handle, so the standard `dataSource` facet costs nothing
        # beyond this open. Without it #23 reconcile has no URI to read and reports every live table
        # `missing_on_storage`, and an event-driven consumer receives a table id it cannot resolve —
        # `services/maintenance` holds no catalog client by design.
        uri = str(getattr(dataset, "uri", "") or "") or None
    except BaseException as exc:  # noqa: BLE001 — a Rust panic is no Exception; is_fault passes the rest
        if not is_fault(exc):
            raise
        log.warning("lineage_readback_failed", extra={"table": table_id, "error": str(exc)}, exc_info=exc)
        return CommitReadback(version=pin_version, schema_fields=[], uri=None)
    if pin_version is None:
        return CommitReadback(version=None, schema_fields=[], uri=uri)
    when = commit_time(dataset)
    try:
        return CommitReadback(version=pin_version, schema_fields=facet_fields(dataset.schema), uri=uri, committed_at=when)
    except BaseException as exc:  # noqa: BLE001 — a Rust panic is no Exception; is_fault passes the rest
        if not is_fault(exc):
            raise
        log.warning("schema_facet_read_failed", extra={"table": table_id, "error": str(exc)}, exc_info=exc)
        return CommitReadback(version=pin_version, schema_fields=[], uri=uri, committed_at=when)


def commit_time(dataset: lance.LanceDataset) -> str | None:
    """When the version ``dataset`` is opened at was committed, ISO 8601 in UTC, off its manifest; ``None`` when unread."""
    try:
        when = manifest_bytes.commit_time(dataset._ds.serialized_manifest())  # noqa: SLF001 — pylance exposes the manifest only here
    except BaseException as exc:  # noqa: BLE001 — a Rust panic is no Exception; is_fault passes the rest
        if not is_fault(exc):
            raise
        log.warning("commit_time_unread", extra={"uri": str(getattr(dataset, "uri", "")), "error": str(exc)}, exc_info=exc)
        return None
    return when.isoformat() if when is not None else None


#: How far below a branch's head :func:`committed_version` looks for a transaction. The commit being
#: identified has just landed, so it sits at the head unless other writers committed after it; the bound
#: keeps a lookup on a busy branch from walking its whole history.
_TRANSACTION_SEARCH_DEPTH: Final = 64


def committed_version(table: table_claims.DoorTable, so: StorageOptions, transaction_id: str | None, *, branch: str | None) -> int | None:
    """The version the transaction ``transaction_id`` committed on ``branch`` of the door's ``table``, or ``None`` when it cannot be found.

    For the native ops whose response carries a ``transaction_id`` and no version (restore, schema
    metadata, the index doors): the spec's ``DescribeTransaction`` answers ``properties.version``
    (``spec.yaml`` ``/v1/transaction/{id}/describe``), the version that transaction made rather than the
    one the table is at now: a commit event is built from its own transaction's context, never from a re-read,
    because a re-read can see a later writer's version.

    A BRANCH IS SEARCHED ON ITS OWN HANDLE, because the ``dir`` backend's ``describe_transaction`` reads
    main's history only: measured on pylance 12.0.0, a restore on branch ``x`` answered a transaction id
    that ``describe_transaction`` reports ``TransactionNotFound``, while ``read_transaction`` on the
    branch's head returned that uuid. The walk starts at the head and stops at the bound or above the
    branch point, the first version the branch did not commit.

    Main's lookup names the table, which only that table's own history answers, since the transaction id is the
    commit's; a branch's opens the location the door resolved. Read after the write, so neither asks whether the
    name still resolves there.

    Best-effort: ``None`` makes the event versionless, never the request a failure.
    """
    if not transaction_id:
        return None
    table_id = table.segments
    try:
        if recorded_branch(branch) is None:
            described = table.namespace.describe_transaction(DescribeTransactionRequest(id=[*table_id, transaction_id]))
            raw = (described.properties or {}).get("version")
            return int(raw) if raw is not None else None
        dataset = open_resolved_dataset_unchecked(table.location, so, table_id, branch=branch)
        head = int(dataset.version)
        # THE WALK STOPS ABOVE THE BRANCH POINT. A branch's first manifest carries the parent's version
        # number and the `Clone` that made the branch, never a write's transaction (measured on pylance
        # 13.0.0), and one version lower raises OSError (no manifest of the branch's own). Nothing at or
        # below `parent_version` is this branch's commit, so neither is read.
        listed = dataset.branches.list().get(cast(str, recorded_branch(branch))) or {}
        parent = listed.get("parent_version")
        floor = max(head - _TRANSACTION_SEARCH_DEPTH, int(parent) if isinstance(parent, int) else 0)
        for version in range(head, floor, -1):
            transaction = read_transaction(dataset, version)
            if transaction is not None and transaction.uuid == transaction_id:
                return version
    except BaseException as exc:  # noqa: BLE001 — a Rust panic is no Exception; is_fault passes the rest
        # This runs after the write committed, so an escaped panic answers 500 for a commit that happened and skips the
        # door's idempotency record, and a retry commits again.
        if not is_fault(exc):
            raise
        log.warning(
            "committed_version_unreadable", extra={"table": table_id, "branch": branch, "error": str(exc), "error_type": type(exc).__name__}, exc_info=exc
        )
    return None


def branch_identifier(location: str, so: StorageOptions, table_id: list[str], *, branch: str | None) -> str | None:
    """The identifier of the CURRENT incarnation of ``branch`` of the table at ``location``, or ``None`` for main or when unreadable.

    pylance 12's ``branches.list()`` carries ``branch_identifier``, the version mapping stored in
    ``_refs/branches/<b>.json``: a list of ``(parent version, uuid)`` pairs from the first branch off main
    down to this one, whose LAST uuid is minted when this branch is created. Measured on 12.0.0: it
    differs across a delete-and-recreate within the same second while ``parentVersion`` and ``createAt``
    are identical, and a nested branch extends its parent's list by one pair.

    Best-effort, like the rest of the lineage enrichment it feeds.
    """
    name = recorded_branch(branch)
    if name is None:
        return None
    try:
        entry = open_resolved_dataset_unchecked(location, so, table_id).branches.list().get(name)
    except BaseException as exc:  # noqa: BLE001 — a Rust panic is no Exception; is_fault passes the rest
        if not is_fault(exc):
            raise
        log.warning("branch_identifier_unreadable", extra={"table": table_id, "branch": name, "error": str(exc)}, exc_info=exc)
        return None
    return identifier_of(entry)


#: The key a branch's identifier is published under: in ``list_branches``' per-branch ``metadata`` and in
#: the ``extra`` of the branch control events. pylance's own field name, so it reads the same everywhere.
BRANCH_IDENTIFIER_KEY: Final = "branch_identifier"


def tag_event_ref(location: str, so: StorageOptions, table_id: list[str], *, branch: str | None) -> dict[str, str]:
    """The ref a tag pins on the table at ``location``, for a tag event's ``extra``: nothing for main, else the branch and
    its identifier.

    A tag names a version WITHIN its branch's history, and branch histories are numbered independently, so ``version``
    alone names a different snapshot on main. The identifier says which incarnation of the branch: a recreated branch
    restarts its numbering. Absent for main, as the lineage event's ``ref`` is.
    """
    ref = recorded_branch(branch)
    if ref is None:
        return {}
    identifier = branch_identifier(location, so, table_id, branch=ref)
    return {"branch": ref, **({BRANCH_IDENTIFIER_KEY: identifier} if identifier is not None else {})}


def identifier_of(entry: object) -> str | None:
    """The last uuid of a listed branch's ``branch_identifier`` chain: the one that names this incarnation."""
    chain = entry.get("branch_identifier") if isinstance(entry, dict) else None
    if not isinstance(chain, list) or not chain:
        return None
    last = chain[-1]
    return str(last[1]) if isinstance(last, tuple | list) and len(last) == 2 else None


def payload_schema_fields(schema: pa.Schema, table_id: list[str]) -> SchemaFields:
    """Schema-facet fields read off the create payload's schema (no storage round trip).

    A create/Overwrite writes exactly this payload, so its schema — including the blob/vector field
    metadata ``facet_fields`` keys on — IS the new table's schema; re-opening the just-written dataset
    would cost a describe + object-store open for information already in memory. NOT valid for ExistOk
    (which may keep an existing table the payload never touched) — that path reads back pinned instead.
    ``table_id`` is logging context only. Best-effort (``[]`` on a failure): the schema facet is an
    enrichment, never a reason to fail the write.
    """
    try:
        return facet_fields(schema)
    except BaseException as exc:  # noqa: BLE001 — a Rust panic is no Exception; is_fault passes the rest
        if not is_fault(exc):
            raise
        log.warning("schema_facet_parse_failed", extra={"table": table_id, "error": str(exc)}, exc_info=exc)
        return []


def commit_ref_key(location: str, so: StorageOptions, table_id: list[str], *, branch: str | None) -> str:
    """The key the commit reconciler reads ``branch`` (``None`` = main) of the table at ``location`` by
    (:func:`commit_records.ref_key`): ``""`` for main, else the branch and its current incarnation, read off the table's
    branch listing as a pass reads it.

    Raises:
        ServiceUnavailableError: The object store did not answer listing the table's branches, so no record is keyed.
        TableChangedError: The location holds no readable dataset any more: the table changed while the request was served.
    """
    if branch is None:
        return ""
    try:
        listed = open_resolved_dataset_unchecked(location, so, table_id).branches.list() or {}
    except (OSError, ValueError) as exc:
        if reads_as_store_outage(exc):
            raise ServiceUnavailableError("the object store did not answer listing the table's branches") from exc
        log.warning("commit_ref_unread", extra={"table": table_id, "location": location, "error": str(exc)[:300]})
        raise table_claims.TableChangedError("the table changed while the request was served: its branches could not be listed") from exc
    return commit_records.ref_key(branch, identifier_of(listed.get(branch)))


#: Per-operation fields worth surfacing in a commit log, keyed by the pylance operation class name. Read
#: off the real objects rather than guessed — probed against pylance 8.0.0 with a create → append → delete
#: → update sequence, which is where the shape of each of these came from:
#:
#:   Overwrite  fragments, new_schema           (a create: "the table came into being with these columns")
#:   Append     fragments                       ("N fragments arrived")
#:   Delete     predicate, updated_fragments,   ("rows matching `id = 2` went away") — the predicate is the
#:              deleted_fragment_ids             single most useful field in the whole log
#:   Update     update_mode, fields_modified,   ("these fields were rewritten, this way")
#:              updated_fragments, new_fragments
#:
#:   Restore    version                          ("this version's content is version N's") — see the rename
#:              below; the field name collides with the row's own key
#:   CreateIndex new_indices, removed_indices     ("an index was built/dropped here")
#:   Rewrite    groups, rewritten_indices         (compaction: "14 groups were rewritten")
#:   Project    schema                            (drop_columns)
#:   Merge      fragments, schema                 (add_columns)
#:
#: Anything not listed but MODELLED still gets its operation NAME, so a Merge/Compact/CreateIndex commit
#: appears in the log as itself rather than vanishing — an operation missing from this table is a gap in
#: the table, never a gap in the history. An operation pylance models no subclass for cannot be named at
#: all and reports a null operation, like a transaction that cannot be read.
#:
#: Verified against pylance 8.0.0 by reading ``__dataclass_fields__`` off each ``lance.LanceOperation``
#: class rather than trusting the docs, because the whole value of this endpoint is reporting what Lance
#: actually recorded.
_TXN_DETAIL_FIELDS = (
    "predicate",
    "update_mode",
    "fields_modified",
    "updated_fragments",
    "new_fragments",
    "removed_fragment_ids",
    "deleted_fragment_ids",
    "fragments",
    "new_indices",
    "removed_indices",
    "groups",
    "version",
)


#: Operation fields whose own name collides with a row key, or reads wrong out of context.
#:
#: ``Restore.version`` is the dangerous one and the reason this map exists. Restore mints a *fresh* version
#: whose content equals an older one (``api/v1/endpoints/tables.py`` — restore commits rather than rewinds),
#: so the log shows v12 with ``operation: "Restore"`` and, without this field, no clue what it restored. But
#: copying it through under its own name would overwrite ``row["version"]`` — the log's primary key and the
#: join key for the lineage actor — turning "v12 restored from v7" into a row claiming to *be* v7. The most
#: valuable missing field and a silent corruption share one name, so it is renamed on the way out.
_TXN_FIELD_RENAMES: Final = frozendict({"version": "restored_from"})


def table_history(table: table_claims.DoorTable, so: StorageOptions, limit: int = 50) -> list[dict[str, Any]]:
    """The table's commit log: one row per version, newest first — what changed, and when.

    Lance is immutable and append-only at the manifest level, so the history is not something we have to
    keep a side-table for: ``versions()`` supplies the WHEN and the transaction log supplies the WHAT
    (operation kind, the delete predicate as the caller wrote it, which fields an update rewrote, fragment
    deltas). This reads both and joins them by version.

    It deliberately does NOT answer WHO. Lance's transaction log has no notion of a user and should not —
    identity is our concern, not the format's. The actor per version lives in the lineage store's
    ``rask_author`` run facet (``GET /datasets/{name}/producers`` returns ``dataset_version`` + ``author`` +
    ``operation``), so a caller that wants who/when/what joins this with that on the version number. Two
    sources, each authoritative for its own half; inventing a third would mean keeping a copy of one of
    them in sync.

    ``limit`` bounds the transaction reads, not the versions list: a table with 10k versions should not
    become 10k object-store round trips because a UI asked for a page.
    """
    dataset = table.open_unchecked(so)
    versions = sorted(dataset.versions(), key=lambda v: int(v["version"]), reverse=True)[:limit]
    out: list[dict[str, Any]] = []
    for entry in versions:
        version = int(entry["version"])
        row: dict[str, Any] = {
            "version": version,
            "timestamp": committed_at(entry).isoformat(),
            "operation": None,
        }
        try:
            txn = read_transaction(dataset, version)
        except Exception as exc:  # noqa: BLE001 — one version's failure is that version's row, never the door's
            # Versions written before transaction files, or a GC'd txn, still belong in the log. Reporting
            # the version with a null operation is honest; dropping it would make the history lie by omission.
            # ONE VERSION, NOT THE DOOR: whatever kept this version's transaction from being read or converted (an
            # ``OSError`` for a manifest gone, truncated or not one, measured on pylance 14.0.3; a namespace error for
            # a store an open connection lost; an operation this binding cannot convert), the version is listed with
            # a null operation and the failure logged by its type and text: one line per version per history request,
            # so the version's row is the record, and a traceback would repeat the same frames for every one.
            log.info("txn_unreadable", extra={"table": table.segments, "version": version, "error": str(exc), "error_type": type(exc).__name__})
            out.append(row)
            continue
        op = getattr(txn, "operation", None)
        # An operation this binding models no subclass for reaches here as the ABC itself, so
        # `type(op).__name__` would answer `BaseOperation` — the name of an abstract base class, not of
        # anything Lance recorded. One `compact_files()` commits such a version before its `Rewrite`
        # (measured 2026-09-11), so every compacted table would carry a row naming an operation no Lance
        # release defines. Null is the same answer the unreadable-transaction branch above already gives
        # to the same question, and needs no vocabulary a client must learn.
        if op is None or type(op) is lance.LanceOperation.BaseOperation:
            out.append(row)
            continue
        row["operation"] = type(op).__name__
        for field in _TXN_DETAIL_FIELDS:
            if not hasattr(op, field):
                continue
            value = getattr(op, field)
            # Fragment and index lists are long and carry no meaning in a log — the COUNT is the signal.
            row[_TXN_FIELD_RENAMES.get(field, field)] = len(value) if isinstance(value, (list, tuple)) else value
        # Did this version change the columns? Both attribute names must be tested: only ``Overwrite`` (a
        # create) carries ``new_schema``, while ``drop_columns`` commits a ``Project`` and ``add_columns``
        # commits a ``Merge`` — and both of those carry plain ``schema``. Testing only ``new_schema``
        # reported ``schema_set: false`` for a column drop, which is the log denying the one kind of change
        # a reader is most likely to be hunting for.
        row["schema_set"] = any(getattr(op, name, None) is not None for name in ("new_schema", "schema"))
        out.append(row)
    return out
