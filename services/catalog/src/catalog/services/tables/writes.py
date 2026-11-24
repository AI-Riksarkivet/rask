"""The data doors' commits, from the request body to the version it made: insert, merge-insert, update, delete, restore
and an index's drop.

Most take the table a door resolved (``table_claims.DoorTable``) and the storage options."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final, cast

import lance
import pyarrow as pa
from lance import FragmentMetadata, Transaction
from lance_namespace import (
    DeleteFromTableRequest,
    DeleteFromTableResponse,
    DropTableIndexResponse,
    InsertIntoTableRequest,
    InsertIntoTableResponse,
    InvalidInputError,
    MergeInsertIntoTableRequest,
    MergeInsertIntoTableResponse,
    RestoreTableRequest,
    RestoreTableResponse,
    TableIndexNotFoundError,
    UnsupportedOperationError,
    UpdateTableRequest,
    UpdateTableResponse,
)
from pydantic import BaseModel

from catalog.core import formats, primary_keys, provenance_guard
from catalog.core.lance_refusals import caller_sql, write_schema_errors
from catalog.core.modes import InsertMode
from catalog.core.namespace import disarm_commit_path_cleanup
from catalog.core.query_bounds import refuse_an_unbounded_boolean_chain
from catalog.core.run_context import AUTHOR_PROPERTY, IDEMPOTENCY_ANSWER_PROPERTY, IDEMPOTENCY_KEY_PROPERTY, OPERATION_PROPERTY
from catalog.core.version_retention import recorded_branch
from catalog.services import commit_authors, committed_files, table_claims
from catalog.services.cast_size import bytes_after_cast
from catalog.services.tables.commits import captured, commit_stamped
from catalog.services.tables.versions import commit_ref_key
from lakehouse.arrow_ipc import ArrowBodyError, ArrowBodyTooLargeError, decode_arrow_body, encode_arrow_stream
from lakehouse.lancekit import auto_cleanup
from lakehouse.lancekit.transactions import read_transaction
from lakehouse.lancekit.versions import committed_at
from lakehouse.storage.options import StorageOptions


__all__ = [
    "SYSTEM_INDICES",
    "coerce_insert_arrow",
    "committed_under_key",
    "delete_from_table",
    "drop_index",
    "insert_into_table",
    "merge_insert_into_table",
    "read_arrow_body",
    "refuse_a_restore_carrying_refused_config",
    "restore",
    "update_table",
]


def read_arrow_body(data: bytes, *, max_bytes: int) -> pa.Table:
    """A write body as a table, or `InvalidInputError` (400, code 13) saying it is too large or not a valid Arrow IPC stream.

    Decoded by the fleet's one validating decoder (`lakehouse.arrow_ipc`): read whole, then
    validated in full, because framing that parses says nothing about the buffers — Lance writes an
    offset past its values buffer as bytes from outside the request (pyarrow 25.0.0, pylance 12.0.0).
    ``max_bytes`` is the catalog's body cap (``LANCE_MAX_BODY_BYTES``), which the body may not exceed
    once its compressed buffers inflate: the write load-shed sizes its concurrency as if each write held
    at most that much (``values-prod.yaml``, ``catalog.maxConcurrentWrites``), and the native backend
    inflates the same bytes again on the main arm. :func:`coerce_insert_arrow` holds the rows it
    re-encodes to the same cap.
    """
    try:
        rows = decode_arrow_body(data, max_bytes=max_bytes, framing="stream")
    except ArrowBodyTooLargeError as exc:
        raise InvalidInputError(
            f"the request body is too large once decoded: {exc}; write large payloads directly to object storage with vended credentials"
            " instead of through the catalog"
        ) from exc
    except ArrowBodyError as exc:
        raise InvalidInputError(f"the request body is not a valid Arrow IPC stream: {exc}") from exc
    _refuse_values_that_are_not_json(rows)
    return rows


def _refuse_values_that_are_not_json(rows: pa.Table) -> None:
    """Every JSON value in ``rows`` parses as JSON, wherever its column nests it.

    Lance parses a JSON value only when it encodes it, deep in the write, and reports one that does not parse as an
    untyped ``OSError`` ("Failed to encode JSON"), which a door answered 500. Measured on pylance 13.0.0, it encodes a
    JSON value beneath a struct, a list, a large list and a map, and beneath a null struct or a null list too, so every
    child slot is read here, whatever its parent's validity. Each chunk is read as it came: pyarrow cannot combine an
    extension column of no chunks, an empty blob column among them (measured on pyarrow 25.0.0).

    Raises:
        InvalidInputError: A JSON value does not parse; the detail names its path and its position among that path's
            values, which for a column outside every list is its row.
    """
    for field in rows.schema:
        seen: Counter[str] = Counter()
        for chunk in rows.column(field.name).chunks:
            for path, values in _json_values(field.name, chunk):
                _require_json(path, values, first=seen[path])
                seen[path] += len(values)


def _require_json(path: str, values: pa.ExtensionArray, *, first: int) -> None:
    """Refuse a value of the JSON array ``values`` at ``path`` that does not parse, its position counted from ``first``."""
    for position, value in enumerate(values.storage.to_pylist(), start=first):
        if value is None:
            continue
        try:
            json.loads(value)
        except ValueError as exc:
            raise InvalidInputError(f"column {path!r} holds a value that is not JSON at position {position}: {exc}") from exc


#: The nested types whose ``values`` hold one child array: every list, and a map, whose child is its key and value struct.
_LIST_LIKE: Final[tuple[Callable[[pa.DataType], bool], ...]] = (
    pa.types.is_list,
    pa.types.is_large_list,
    pa.types.is_fixed_size_list,
    pa.types.is_list_view,
    pa.types.is_large_list_view,
    pa.types.is_map,
)


def _json_values(path: str, values: pa.Array) -> Iterator[tuple[str, pa.ExtensionArray]]:
    """Each JSON array within ``values``, by its path (``.`` into a struct field, ``[]`` into a list's or a map's values)."""
    kind = values.type
    if isinstance(kind, pa.JsonType):
        yield path, cast("pa.ExtensionArray", values)
    elif pa.types.is_struct(kind):
        struct = cast("pa.StructArray", values)
        for index, field in enumerate(kind):
            yield from _json_values(f"{path}.{field.name}", struct.field(index))
    elif any(is_kind(kind) for is_kind in _LIST_LIKE):
        yield from _json_values(f"{path}[]", cast("pa.ListArray", values).values)


def update_table(table: table_claims.DoorTable, so: StorageOptions, req: UpdateTableRequest, *, transaction_properties: dict[str, str]) -> UpdateTableResponse:
    """Apply SQL ``[path, expression]`` updates to matching rows, committed with ``transaction_properties`` ([[LH-497]]).

    Lance's own update, captured and committed stamped (:func:`captured`): it takes no transaction properties. The rows
    it updates are the rows its predicate matches on the version it was planned on, counted on that version before it
    runs, since the captured call answers nothing.
    """
    updates = dict(req.updates or [])
    if not updates:
        raise InvalidInputError("update requires at least one [path, expression] pair")
    # `branch=` is load-bearing and was MISSING. The request carries it, the served OpenAPI advertises
    # it, and every sibling mutation on this object passes it — so a caller working on a branch mutated
    # MAIN, got a 200, and the lineage WROTE edge recorded main's version. A branch exists precisely so
    # work can be staged without touching main; silently writing to main is the one outcome that makes
    # the feature worse than not having it.
    refuse_an_unbounded_boolean_chain(req.predicate, field="predicate")
    for expression in updates.values():
        refuse_an_unbounded_boolean_chain(expression, field="updates")
    where = req.predicate

    def matched(handle: lance.LanceDataset) -> int:
        return int(handle.count_rows(filter=where) if where else handle.count_rows())

    with caller_sql("invalid update expression or predicate"):
        version, updated = commit_stamped(
            table,
            so,
            transaction_properties,
            plan=_answering(
                transaction_properties,
                captured(table, so, lambda handle: handle.update(updates, where=where), branch=req.branch, measure=matched),
                lambda matched_rows: {"updated_rows": matched_rows or 0},
            ),
            branch=req.branch,
        )
    return UpdateTableResponse(updated_rows=updated or 0, version=version)


def delete_from_table(
    table: table_claims.DoorTable, so: StorageOptions, req: DeleteFromTableRequest, *, transaction_properties: dict[str, str]
) -> DeleteFromTableResponse:
    """Delete rows matching the request predicate, committed with ``transaction_properties`` ([[LH-497]])."""
    # Same omission as `update_table` above, and worse here: a wrong-target DELETE loses rows that were
    # never meant to be touched, and returns 200.
    refuse_an_unbounded_boolean_chain(req.predicate, field="predicate")
    predicate = req.predicate
    with caller_sql("invalid delete predicate"):
        version, _ = commit_stamped(
            table,
            so,
            _with_answer(transaction_properties, {}),
            plan=captured(table, so, lambda handle: handle.delete(predicate), branch=req.branch),
            branch=req.branch,
        )
    return DeleteFromTableResponse(version=version)


def _with_answer(properties: dict[str, str], answer: dict[str, int]) -> dict[str, str]:
    """``properties`` carrying ``answer`` beside the key when they carry one (:data:`IDEMPOTENCY_ANSWER_PROPERTY`), so a
    takeover of the key answers from the commit as this write answers."""
    if IDEMPOTENCY_KEY_PROPERTY not in properties:
        return properties
    return {**properties, IDEMPOTENCY_ANSWER_PROPERTY: json.dumps(answer, sort_keys=True)}


def _answering[T](
    properties: dict[str, str], plan: Callable[[lance.LanceDataset], tuple[Transaction, T]], answer: Callable[[T], dict[str, int]]
) -> Callable[[lance.LanceDataset], tuple[Transaction, T]]:
    """``plan``, whose transaction carries the answer its measurement gives (:func:`_with_answer`) when ``properties``
    carry a key: an update's or a merge's counts are known only once it is planned, and each plan measures again."""
    if IDEMPOTENCY_KEY_PROPERTY not in properties:
        return plan

    def answered(dataset: lance.LanceDataset) -> tuple[Transaction, T]:
        transaction, measured = plan(dataset)
        stamped = {**(transaction.transaction_properties or {}), IDEMPOTENCY_ANSWER_PROPERTY: json.dumps(answer(measured), sort_keys=True)}
        return replace(transaction, transaction_properties=stamped), measured

    return answered


#: How far before a lapsed attempt's claim its commit is looked for: the commit's time is the pod's clock and the claim's
#: the store's, so the search starts this much earlier than the claim, wider than any skew between them.
_CLOCK_SKEW = timedelta(minutes=10)


def committed_under_key[R: BaseModel](
    table: table_claims.DoorTable, so: StorageOptions, *, branch: str | None, mark: str, since: datetime, answer: type[R]
) -> R | None:
    """What the write an earlier attempt under the key ``mark`` committed on ``branch`` answered, or ``None`` when it
    committed none.

    For a key taken over from an attempt whose lease ran out unanswered ([[LH-577]]): it may have committed and been lost
    before it recorded its answer, and a write run again would apply twice. Its commit carries the key
    (:data:`IDEMPOTENCY_KEY_PROPERTY`) and its answer (:data:`IDEMPOTENCY_ANSWER_PROPERTY`), so the versions committed
    since that attempt claimed the key are read, newest first, until one older than the claim, and the one carrying the
    key is answered as ``answer``: that version and the answer stamped beside the key. Lance keeps a version's
    transaction until a cleanup removes it, and a version committed minutes ago is far inside any retention.
    """
    dataset = table.open(so, branch=branch)
    earliest = since - _CLOCK_SKEW
    for entry in reversed(dataset.versions()):
        if committed_at(entry) < earliest:
            return None
        version = int(entry["version"])
        transaction = read_transaction(dataset, version)
        if transaction is None or (properties := transaction.transaction_properties or {}).get(IDEMPOTENCY_KEY_PROPERTY) != mark:
            continue
        stamped = properties.get(IDEMPOTENCY_ANSWER_PROPERTY)
        if stamped is not None:
            return answer.model_validate({**json.loads(stamped), "version": version})
        recovered = _UNANSWERED.get(answer)
        return answer.model_validate({**(recovered(transaction) if recovered is not None else {}), "version": version})
    return None


def _rows_added(fragments: Sequence[FragmentMetadata] | None) -> int:
    return sum(int(fragment.physical_rows or 0) for fragment in fragments or [])


#: What a door answers, besides the version, for a commit that carries its key and no answer (an image that stamped only
#: the key made it), read off its transaction; a door not named answers the version alone, as a count it cannot read
#: there is left out rather than guessed. An append's operation holds the fragments it added. An update is committed in
#: Lance's ``rewrite_rows`` mode, whose ``new_fragments`` hold the updated rows (measured on pylance 14.0.3, 2026-11-20:
#: three of four rows updated, ``new_fragments`` held 3 rows).
_UNANSWERED: Final[dict[type[BaseModel], Callable[[Transaction], dict[str, int]]]] = {
    InsertIntoTableResponse: lambda transaction: {"num_inserted_rows": _rows_added(getattr(transaction.operation, "fragments", None))},
    UpdateTableResponse: lambda transaction: {"updated_rows": _rows_added(getattr(transaction.operation, "new_fragments", None))},
}


def insert_into_table(
    table: table_claims.DoorTable,
    so: StorageOptions,
    req: InsertIntoTableRequest,
    *,
    data: bytes,
    max_bytes: int,
    transaction_properties: dict[str, str],
) -> InsertIntoTableResponse:
    """Append (or overwrite) Arrow-IPC rows on the ref the request NAMES, and report the commit it made.

    ONE PATH FOR EVERY REF, through a handle opened on that ref. A branch needs it: the `dir` backend's
    `insert_into_table` answers `branch=work` by appending to main (measured on pylance 14.0.3, 2026-11-17: main went
    from 3 rows to 4, the branch kept 3, and the answer named main's version). Main takes the same path because the
    door's stamp and a keyed append ride the commit, which the backend's op carries neither of. The spec's
    `InsertIntoTableResponse.version` is "the commit version associated with the operation", and a reopen cannot
    recover that number: under concurrent appends it reports whichever commit landed last. `LanceDataset.insert`
    advances its own handle to the version IT committed, after any conflict rebase (measured on 12.0.0: two handles
    appending in turn report v3 and v4, each holding its own rows), so the response and the lineage event name the
    write that happened.

    The handle is opened through the checked open ([[LH-279]]), so the manifest the write builds on is
    the one judged. `mode` is read through `InsertMode`, whose two values are pylance's own spellings of
    the spec's two modes.

    On a table declaring a primary key, rows repeating a key among themselves are refused, and an append
    lands through Lance's `merge_insert(...).when_matched_fail().when_not_matched_insert_all()`, refused
    when the table holds one of its keys as it commits ([[LH-243]], :mod:`catalog.core.primary_keys`).

    ``transaction_properties`` (the door's stamp, :mod:`catalog.core.run_context`) ride the commit, on any ref: a keyed
    append is finalized on the ref it planned on (:func:`commit_stamped`), and Lance's own insert takes them.
    """
    # Validated here as well as at the door's coercion: this hands pyarrow's buffers to Lance in-process,
    # and Lance writes whatever an unvalidated offset points at.
    rows = read_arrow_body(data, max_bytes=max_bytes)
    transaction_properties = _with_answer(transaction_properties, {"num_inserted_rows": rows.num_rows})
    dataset = disarm_commit_path_cleanup(table.open(so, branch=req.branch), table.segments)
    # THROUGH `InsertMode`, the one vocabulary every caller shares. pylance's own parser is not the
    # spec's: it takes `create`, which the spec does not give this door, and refuses an unknown value with
    # a bare ValueError that would answer 500. The parse is idempotent, so a mode the door already parsed
    # passes through unchanged.
    mode = InsertMode.parse(req.mode)
    keys = primary_keys.key_columns(dataset.lance_schema)
    primary_keys.refuse_repeats_within(rows, keys, door="insert")
    with write_schema_errors():
        keyed_append = mode is InsertMode.APPEND and primary_keys.joinable(rows, keys)
        if keyed_append:
            # Planned on the latest version and finalized as exactly the next, so a racing insert of the same key is
            # planned again and refused ([[LH-243]]).
            version, _ = commit_stamped(
                table, so, transaction_properties, plan=lambda latest: primary_keys.plan_new_keys(latest, rows, keys, door="insert"), branch=req.branch
            )
        else:
            # An append or overwrite commutes with any racing append, so Lance's own rebase keeps it whole. Lance commits it
            # itself, so it waits out another commit's turn here rather than at a finalize ([[LH-259]]); the wait refuses a
            # table no uuid identifies, which takes no write ([[LH-534]]), and its files are recorded under the uuid once it lands.
            table.wait_for_turn(recorded_branch(req.branch))
            if table.announces(transaction_properties.get(OPERATION_PROPERTY, "")):
                table.file_pending_commit()
            dataset.insert(rows, mode=mode.value, transaction_properties=transaction_properties)
            version = int(dataset.version)
            transaction = committed_files.record_landed(table.record_committed_files, dataset, table_location=table.location, branch=req.branch, so=so)
            author = transaction_properties.get(AUTHOR_PROPERTY)
            if author is not None and table.announces(transaction_properties.get(OPERATION_PROPERTY, "")):
                # The ref's key is read inside the recorder, so a store that does not answer it is a counted miss: the
                # version has landed, and an error would have the caller append it again.
                commit_authors.record_landed(
                    lambda transaction_uuid, landed: table.record_author(
                        transaction_uuid,
                        version=landed,
                        ref=commit_ref_key(table.location, so, table.segments, branch=recorded_branch(req.branch)),
                        author=author,
                        manifest=None,
                    ),
                    transaction,
                    version=version,
                    table_location=table.location,
                )
    # The rows this request wrote, read off its own payload: a row count diff across the commit would
    # include whatever a concurrent writer appended in between.
    return InsertIntoTableResponse(version=version, num_inserted_rows=rows.num_rows)


def merge_insert_into_table(
    table: table_claims.DoorTable,
    so: StorageOptions,
    req: MergeInsertIntoTableRequest,
    *,
    data: bytes,
    max_bytes: int,
    transaction_properties: dict[str, str],
) -> MergeInsertIntoTableResponse:
    """Run the spec's merge-insert against the ref the request NAMES, on the handle opened at the door's location.

    ``req.on`` is the merge key, every field path the request names, and a row matches only when every field is equal
    ([[LH-537]]).

    In-process on every ref, main included: the native merge names the table and would resolve it again ([[LH-530]]),
    and it runs the same ``MergeInsertBuilder`` on the dataset it loads (lance-namespace-impls 13.0.0, ``dir.rs``),
    answering no transaction id either.

    Same defect and same severity as `insert_into_table`: verified live, a merge naming `work` applied
    its update to MAIN and left the branch untouched, reporting `num_updated_rows: 1` for a row it had
    not changed on the dataset the caller asked about.

    Every spec parameter has a `MergeInsertBuilder` method of the same name, so the mapping is
    transcription rather than interpretation — which is exactly why this door is HONOURED where `query`
    is refused. There is no vector search, no full-text query and no index-selection surface here to
    re-derive and get subtly wrong.

    ``transaction_properties`` (the door's stamp, :mod:`catalog.core.run_context`) ride the commit, on any ref: the merge
    is planned uncommitted and finalized on the ref it planned on (:func:`commit_stamped`), because the native merge takes
    no transaction properties.
    """
    # BEFORE THE MERGE. Lance in-process is handed pyarrow's buffers, where an offset past its values
    # buffer is written as process heap and a decreasing one crashes the process (measured on pylance 12.0.0),
    # so both filters answer 400 here.
    refuse_an_unbounded_boolean_chain(req.when_matched_update_all_filt, field="when_matched_update_all_filt")
    refuse_an_unbounded_boolean_chain(req.when_not_matched_by_source_delete_filt, field="when_not_matched_by_source_delete_filt")
    rows = read_arrow_body(data, max_bytes=max_bytes)
    dataset = disarm_commit_path_cleanup(table.open(so, branch=req.branch), table.segments)
    # A source repeating a key either inserts every copy (unmatched) or is ambiguous (matched); both arms ([[LH-243]]).
    primary_keys.refuse_repeats_within(rows, primary_keys.key_columns(dataset.lance_schema), door="merge_insert")
    # THE BUILDER IS CONSTRUCTED INSIDE THE GUARD, and that placement is the fix rather than a tidy-up:
    # `merge_insert(on)` is where Lance rejects a key column that does not exist, and it sat outside
    # `caller_sql`, so the one door whose whole job is matching on that column reported `Internal 18`
    # for naming it wrongly — while the branchless path answered 13.
    with write_schema_errors(), caller_sql("invalid merge_insert key or filter"):
        version, stats = commit_stamped(
            table,
            so,
            transaction_properties,
            plan=_answering(transaction_properties, lambda latest: _merge_builder(latest, req).execute_uncommitted(rows), _merge_counts),
            branch=req.branch,
        )
    return MergeInsertIntoTableResponse.model_validate({**_merge_counts(stats), "version": version})


def _merge_counts(stats: object) -> dict[str, int]:
    """The row counts a merge answers, off the statistics its uncommitted execution reports."""
    counts = stats if isinstance(stats, dict) else {}
    return {name: int(counts.get(name, 0)) for name in ("num_updated_rows", "num_inserted_rows", "num_deleted_rows")}


def _merge_builder(dataset: lance.LanceDataset, req: MergeInsertIntoTableRequest) -> lance.MergeInsertBuilder:
    """The merge ``req`` asks for on its key ``on``, on ``dataset``; every spec parameter has a builder method of the same name.

    Read as ``spec.yaml`` states them: each ``*_filt`` is its clause's own condition, so a filter applies whether or not
    its flag is set, and ``when_not_matched_insert_all`` defaults to false. The ``dir`` backend's native merge reads an
    absent ``when_not_matched_insert_all`` as an insert (lance-namespace-impls 13.0.0, ``dir.rs``), which the spec's
    default is not.
    """
    if not req.on:
        raise InvalidInputError("merge_insert needs its key: name at least one field path in on")
    builder = dataset.merge_insert(req.on)
    if req.when_matched_update_all or req.when_matched_update_all_filt is not None:
        builder.when_matched_update_all(req.when_matched_update_all_filt)
    if req.when_not_matched_insert_all:
        builder.when_not_matched_insert_all()
    if req.when_not_matched_by_source_delete or req.when_not_matched_by_source_delete_filt is not None:
        builder.when_not_matched_by_source_delete(req.when_not_matched_by_source_delete_filt)
    if req.use_index is not None:
        builder.use_index(req.use_index)
    return builder


def refuse_a_restore_carrying_refused_config(target: lance.LanceDataset, *, version: int) -> None:
    """Refuse restoring ``target``, the version a restore would commit again, when its config carries ``lance.auto_cleanup.*``
    ([[LH-245]]) or ``lance.compaction.*`` ([[LH-538]]) keys, which no writer may set.

    A restore commits the restored version's config, and Lance's commit-path cleanup reads the manifest a
    commit produces (measured on pylance 12.0.0: disarming the latest version and then restoring an armed one
    re-armed the table and deleted versions in the restore's own commit). No disarm can precede that commit,
    because the keys arrive with it, so the door refuses rather than letting a restore delete history past
    every hold. Compaction keys would plan the table's next compaction in place of its maintenance policy.

    Raises:
        InvalidInputError: The version to restore carries ``lance.auto_cleanup.*`` or ``lance.compaction.*`` keys.
    """
    config = target.config()
    if armed := auto_cleanup.armed_keys(config):
        raise InvalidInputError(
            f"restore refused: version {version} carries commit-path auto-cleanup config {armed}, and restoring it would delete "
            "table versions inside the restore's own commit. Version reclamation belongs to maintenance; restore a version without these keys"
        )
    if steering := formats.compaction_config_keys(config):
        raise InvalidInputError(
            f"restore refused: version {version} carries compaction config {steering}, which Lance would plan a compaction from in place "
            "of the table's maintenance policy; restore a version without these keys"
        )


def restore(
    table: table_claims.DoorTable,
    so: StorageOptions,
    req: RestoreTableRequest,
    *,
    transaction_properties: dict[str, str],
) -> tuple[RestoreTableResponse, int]:
    """Commit ``req``'s ref at ``req.version`` again as its newest version: the spec's answer, and the version made.

    The spec's ``RestoreTable`` answers a transaction id and no version; the version is the one the restore committed.

    The version is opened at the door's location, where a missing branch answers 22 and a version the ref lacks 11, the
    codes every other door mints, and the restore is Lance's own ``Restore`` operation committed with
    ``transaction_properties`` on the ref's latest version (:func:`commit_stamped`, [[LH-497]]); the native restore names
    the table and would resolve it again ([[LH-530]]). Measured on pylance 13.0.0, a ``Restore`` committed this way leaves
    the rows and schema the handle's own ``restore()`` leaves. Unchecked, because a restore reads no row.

    Raises:
        InvalidInputError: The version to restore carries config no writer may set (auto-cleanup or compaction).
        TableChangedError: The table changed since the door's resolution.
    """
    target = table.open_unchecked(so, version=req.version, branch=req.branch)
    refuse_a_restore_carrying_refused_config(target, version=req.version)
    restored = int(target.version)

    def plan(latest: lance.LanceDataset) -> tuple[Transaction, str]:
        transaction = Transaction(read_version=latest.version, operation=lance.LanceOperation.Restore(restored))
        return transaction, transaction.uuid

    version, transaction_id = commit_stamped(table, so, transaction_properties, plan=plan, branch=req.branch)
    return RestoreTableResponse(transaction_id=transaction_id), version


#: The indices Lance keeps for itself, which the ``dir`` backend's drop refuses as Unsupported: ``is_system_index`` names
#: exactly these (Lance ``rust/lance-table/src/system_index.rs`` at v13.0.0).
SYSTEM_INDICES: Final = frozenset({"__lance_frag_reuse", "__lance_mem_wal"})


def drop_index(table: table_claims.DoorTable, so: StorageOptions, name: str, *, transaction_properties: dict[str, str]) -> DropTableIndexResponse:
    """Drop the index ``name`` from main, committed with ``transaction_properties`` ([[LH-497]]), answering the commit's
    transaction id as the spec's ``DropTableIndex`` does.

    Lance's own ``drop_index`` on the handle opened at the door's location, captured and committed stamped
    (:func:`captured`); the refusals are the ``dir`` backend's own (lance-namespace-impls 13.0.0, ``dir.rs``
    ``drop_table_index``): an index the table does not hold answers TableIndexNotFound, a system index Unsupported.

    Raises:
        TableIndexNotFoundError: The table holds no index named ``name``.
        UnsupportedOperationError: ``name`` is a system index.
    """
    held = {index.name for index in table.open_unchecked(so).describe_indices()}
    if name not in held:
        raise TableIndexNotFoundError(f"Table index not found: the table holds no index named {name!r}")
    if name in SYSTEM_INDICES:
        raise UnsupportedOperationError(f"System index '{name}' cannot be dropped via this API")
    version, _ = commit_stamped(table, so, transaction_properties, plan=captured(table, so, lambda handle: handle.drop_index(name), branch=None))
    made = read_transaction(table.open_unchecked(so, version=version), version)
    return DropTableIndexResponse(transaction_id=made.uuid if made is not None else None)


def coerce_insert_arrow(
    table: table_claims.DoorTable, so: StorageOptions, data: bytes, *, branch: str | None = None, max_bytes: int, mode: InsertMode = InsertMode.APPEND
) -> bytes:
    """Align Arrow-IPC insert rows to the table's schema before the append.

    A client that INFERS types loosely — most importantly the browser's apache-arrow, which infers
    ``float64`` for every JS number — otherwise hits the ``insert_into_table`` append with a ``float64``
    batch against an ``int64`` column, which raises a bare **500** ("Internal Server Error"). That is both
    a broken insert AND the wrong status for a client-side mismatch (browser-driven find 2026-07-21). Here we
    select the table's columns BY NAME (a column the table lacks and a missing one are each refused 400, code 13,
    naming them) and cast each to its real column type — so ``4.0 → 4`` just works — turning a genuinely incompatible payload (e.g. ``4.5`` →
    ``int``, or a non-castable type) into a clean ``400``, never a 500. Blocking IO (opens the dataset for the
    live schema); the caller runs it in a threadpool.

    An already-aligned payload passes through UNTOUCHED: re-serializing it cost two full
    materialisations (the IPC re-encode + ``to_pybytes``) on every insert from a schema-exact client —
    which is every non-browser client — for zero behavioural difference (#141).

    The body is read through :func:`read_arrow_body`, so one that is not a valid Arrow IPC stream is
    refused 400 before the dataset opens, on main and on a branch alike. Rows that would be over
    ``max_bytes`` as the table's types are refused before the cast runs (:mod:`catalog.services.cast_size`),
    so a dictionary decoded once per row cannot allocate past the cap, and the append is never handed a body
    larger than the one the caller was held to.

    AN OVERWRITE (``mode``, the request's) KEEPS THE TABLE'S PROVENANCE ([[LH-208]]). Lance takes an overwrite's schema from
    its payload, metadata included, so a payload naming a reserved key would forge one and a payload
    with none would erase every ``lineage.*`` stamp and the primary key's field metadata (measured on
    pylance 12.0.0, on main and on a branch). A reserved key in the payload is refused, and the body handed on carries
    the table's own schema and field metadata. An append is left alone: Lance discards an append
    payload's schema metadata (measured on the same version).
    """
    incoming = read_arrow_body(data, max_bytes=max_bytes)
    overwrite = mode is InsertMode.OVERWRITE
    if overwrite:
        provenance_guard.refuse_reserved_keys((incoming.schema.metadata or {}).keys(), door="insert?mode=overwrite payload")
        formats.refuse_flag_locking_keys((incoming.schema.metadata or {}).keys(), door="insert?mode=overwrite payload")
    # THE REF THE REQUEST NAMES, never main. This alignment judges the payload's columns against the target, so
    # aligning a branch-targeted insert to main's schema would refuse any column the branch has and main does not, and
    # cast the rest to main's types. A branch whose schema has evolved is the whole reason a branch exists, so this is
    # the ordinary case rather than a corner. Same defect family as `update`/`delete` rewriting main
    # (`test_branch_scoped_mutations_hit_the_branch.py`); it survived here because the door-level gate
    # walks doors that build a branched request model, and this is a helper.
    target = table.open(so, branch=branch).schema
    if [(f.name, f.type, f.nullable) for f in incoming.schema] == [(f.name, f.type, f.nullable) for f in target]:
        if not overwrite or incoming.schema.equals(target, check_metadata=True):
            return data
        body = encode_arrow_stream(pa.Table.from_arrays(incoming.columns, schema=target))
        _refuse_over_the_cap(len(body), max_bytes)
        return body
    present = set(incoming.column_names)
    missing = [f.name for f in target if f.name not in present]
    if missing:
        raise InvalidInputError(f"insert rows are missing column(s): {', '.join(missing)}")
    # A column the table lacks has nowhere to go, and Lance's own append refuses it ("Append with different schema:
    # fields did not match, unexpected=[...]", measured on pylance 13.0.0), so the door refuses it too, by name, rather
    # than discard values a caller believes were stored.
    unexpected = sorted(present - {f.name for f in target})
    if unexpected:
        raise InvalidInputError(f"insert rows carry column(s) the table does not have: {', '.join(unexpected)}")
    selected = pa.table({f.name: incoming.column(f.name) for f in target})
    _refuse_over_the_cap(sum(bytes_after_cast(selected.column(f.name), f.type) for f in target), max_bytes)
    try:
        aligned = selected.cast(target if overwrite else pa.schema([pa.field(f.name, f.type, f.nullable) for f in target]))
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError) as exc:
        raise InvalidInputError(f"insert rows don't match the table schema: {exc}") from exc
    body = encode_arrow_stream(aligned)
    _refuse_over_the_cap(len(body), max_bytes)
    return body


def _refuse_over_the_cap(size: int, max_bytes: int) -> None:
    """Both arms of the insert door read the coerced body again, so it is held to the caller's cap here, once."""
    if size > max_bytes:
        raise InvalidInputError(
            f"the insert rows take {size:,} bytes as the table's column types, over the {max_bytes:,}-byte limit on a body;"
            " send them as those types, or in smaller inserts"
        )
