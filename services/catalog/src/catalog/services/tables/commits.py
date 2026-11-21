"""The commit path every catalog write takes: a transaction planned on the latest version of its ref and committed with
the catalog's stamp, planned again when another writer took that version, and Lance's own operation staged through a
capturing namespace so the catalog finalizes it."""

from __future__ import annotations

import logging
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
from functools import partial
from typing import Any, Final

import lance
import pyarrow as pa
import pyarrow.fs as pafs
from lance import Transaction
from lance_namespace import (
    ConcurrentModificationError,
    CreateTableVersionRequest,
    CreateTableVersionResponse,
    DescribeTableRequest,
    DescribeTableResponse,
    DescribeTableVersionRequest,
    DescribeTableVersionResponse,
    InternalError,
    InvalidInputError,
    LanceNamespace,
    ListTableVersionsRequest,
    ListTableVersionsResponse,
    ServiceUnavailableError,
    TableVersion,
    TableVersionNotFoundError,
)
from pydantic import BaseModel, ConfigDict, SkipValidation

from catalog.core import encoding, manifest_bytes, primary_keys
from catalog.core.config import shared_lance_session
from catalog.core.namespace import disarm_commit_path_cleanup
from catalog.core.run_context import AUTHOR_PROPERTY, OPERATION_PROPERTY
from catalog.core.table_objects import dataset_fs, delete_table_object, discard_detached_manifest, object_store_path
from catalog.core.vending import classified_columns, labelled_fields
from catalog.core.version_retention import recorded_branch
from catalog.services import committed_files, contracts, table_claims
from catalog.services.object_uuids import UnidentifiedObjectError
from catalog.services.tables.versions import commit_ref_key
from lakehouse.lancekit import branch_layout, manifest_names
from lakehouse.lancekit.commit_verdict import CommitVerdict, classify_commit_failure, is_retryable_conflict
from lakehouse.lancekit.lance_session import open_dataset
from lakehouse.lancekit.transactions import read_transaction
from lakehouse.records import commit_records, commit_turns, file_identities
from lakehouse.storage.options import StorageOptions


__all__ = [
    "captured",
    "commit_stamped",
    "committed_manifest_digest",
    "expressions_reading_a_label",
]


log = logging.getLogger(__name__)


#: How many times a stamped commit plans again after another writer took the version it followed.
_STAMPED_COMMIT_ATTEMPTS: Final = 10


#: The wait before the next plan of a commit that lost its race and could not take its ref's turn (``commit_turns.backoff``),
#: so writers racing for the turn's record plan again apart rather than in step: the nine waits ten attempts can make come to
#: 2.75 s in all, and up to half as much again.
_RETRY_WAIT_SECONDS: Final = 0.05


_RETRY_WAIT_CAP_SECONDS: Final = 0.4


def _turn_for_the_next_attempt(table: table_claims.DoorTable, ref: str | None, held: ExitStack, *, turn: str | None, attempt: int) -> str | None:
    """The ref's commit turn the attempt after the lost attempt ``attempt`` plans under: the one the commit holds, else one it
    takes now, held until ``held`` closes. A commit whose take lost to the turn record's other writers plans again without one,
    after a wait; after its last attempt a commit takes nothing and waits for nothing."""
    if turn is not None or attempt + 1 == _STAMPED_COMMIT_ATTEMPTS:
        return turn
    try:
        return held.enter_context(table.commit_turn(ref))
    except commit_turns.CommitTurnBusyError as exc:
        log.warning("commit_turn_busy", extra={"table": table.segments, "ref": ref, "error": str(exc)})
        time.sleep(commit_turns.backoff(attempt, first=_RETRY_WAIT_SECONDS, cap=_RETRY_WAIT_CAP_SECONDS))
        return None


def commit_stamped[T](
    table: table_claims.DoorTable,
    so: StorageOptions,
    properties: dict[str, str],
    *,
    plan: Callable[[lance.LanceDataset], tuple[Transaction, T]],
    branch: str | None = None,
    opened: lance.LanceDataset | None = None,
    keeps_labels: bool = False,
) -> tuple[int, T]:
    """Commit the transaction ``plan`` builds on the latest version of ``branch`` (``None`` = main) with ``properties``;
    return the version.

    ``keeps_labels`` holds a schema door to the rule ``version/create`` applies to a stock commit ([[LH-207]]): a field's
    ``rask.*`` metadata changes only through ``update_field_metadata``, which asks ``can_classify``. Measured on pylance
    13.0.0, a same-type re-type re-mints the field id with no metadata and a drop removes the field, either of which would
    leave a classified table directly vendable; such a commit is discarded before its finalize.

    Every catalog door's commit carries the ``rask.*`` stamp ([[LH-497]]), and most Lance operations take no transaction
    properties (pylance 13.0.0: ``update``, ``delete``, ``add_columns``, ``alter_columns``, ``drop_columns``, both metadata
    updates, ``drop_index`` and ``MergeInsertBuilder.execute``), so the door's transaction is planned, then committed
    here. ``plan`` also returns what it measured (a merge's row counts), handed back beside the version.

    A commit through ``LanceDataset.commit`` would rebase over a writer that landed first, and a merge_insert
    transaction loses its key check on the way through Python (pylance 13.0.0: ``LanceOperation.Update`` carries no
    inserted-rows filter): measured, two racing ``when_matched_fail`` inserts of one key both landed, with
    ``max_retries=0`` as well. So the transaction is committed detached and finalized as exactly the version after the
    one it was planned on, through the backend's put-if-not-exists (the ``version/create`` finalize, which takes the
    branch it names, [[LH-490]]); a lost race plans again on the new latest version, which a planned operation then
    sees. An attempt that cannot land, its finalize lost or a judge refusing it, deletes what it wrote first
    (:func:`_discard_unlanded_attempt`, [[LH-564]]); one whose outcome is unknown keeps everything.

    A COMMIT THAT LOST ITS RACE TAKES ITS REF'S TURN for its remaining attempts ([[LH-259]],
    :mod:`lakehouse.records.commit_turns`), whether Lance refused the plan for a version committed since or the finalize
    lost: a writer committing more often than one plan takes would otherwise win every version, and Lance cannot rebase a
    schema change over an append (``lance_docs/lance/format/table/transaction.md`` § Merge Compatibility). While the turn is
    held every other finalize of the ref waits, so the next plans meet only the writers already past their wait; a commit
    whose take lost to the turn's other writers plans again after a wait that doubles each attempt
    (:func:`_turn_for_the_next_attempt`). Every finalize, this one's included, waits out a turn another commit holds.

    The finalize names the table and no location, so it is the door's guarded native write ([[LH-530]]), asking the name
    again after the plan has written its data files, which can take as long as the rows do. ``opened`` is the ref as the
    door already opened it (:meth:`table_claims.DoorTable.open`), which the first attempt plans on rather than opening it
    again.

    Raises:
        ConcurrentModificationError: Every attempt lost its race.
        TableChangedError: The table changed since the door's resolution.
        InvalidInputError: ``keeps_labels`` and the commit loses or changes a field's ``rask.*`` metadata.
        InvalidInputError: The commit adds or changes a field's encoding knob to one Lance cannot honour ([[LH-246]]).
        InvalidInputError: The commit's new rows repeat a value of the table's primary key, within themselves or with a row it
            carries ([[LH-540]]), as ``version/create`` refuses a stock commit's.
        InvalidTableStateError: The commit breaks the data contract the table holds (ADR 0212).
    """
    table_id = table.segments
    ref = recorded_branch(branch)
    turn: str | None = None
    author = properties.get(AUTHOR_PROPERTY) if table.announces(properties.get(OPERATION_PROPERTY, "")) else None
    commit = _StampedCommit(table=table, so=so, properties=properties, ref=ref, author=author, keeps_labels=keeps_labels)
    with ExitStack() as held:
        for attempt in range(_STAMPED_COMMIT_ATTEMPTS):
            dataset = disarm_commit_path_cleanup(opened if opened is not None else table.open(so, branch=ref), table_id)
            opened = None
            next_turn = partial(_turn_for_the_next_attempt, table, ref, held, turn=turn, attempt=attempt)
            outcome = _attempt_once(commit, dataset, plan, turn=turn, next_turn=next_turn)
            turn = outcome.turn
            if outcome.landed is not None:
                return outcome.landed
        raise ConcurrentModificationError(f"the commit lost the race for the next version {_STAMPED_COMMIT_ATTEMPTS} times; retry it")


class _StampedCommit(BaseModel):
    """What every attempt of one :func:`commit_stamped` call commits with."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # The door's table is a collaborator holding the open namespace, not a value: a test stands in a double for it.
    table: SkipValidation[table_claims.DoorTable]
    so: StorageOptions
    properties: dict[str, str]
    ref: str | None
    author: str | None
    keeps_labels: bool


class _Attempt(BaseModel):
    """One attempt's detached commit, to land as ``version`` under ``turn``."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # pylance's dataset handles and transaction are native objects pydantic cannot validate.
    dataset: SkipValidation[lance.LanceDataset]
    stamped: SkipValidation[Transaction]
    detached: SkipValidation[lance.LanceDataset]
    location: str
    version: int
    finalize_id: int
    turn: str | None


class _AttemptOutcome[T](BaseModel):
    """What one attempt landed (its version and what ``plan`` measured), else ``None``, and the turn the next plans under."""

    model_config = ConfigDict(frozen=True)

    # What ``plan`` measured is the caller's own value, of the caller's own type, handed back as it was given.
    landed: SkipValidation[tuple[int, T] | None]
    turn: str | None


class _Finalized(BaseModel):
    """Whether an attempt's finalize landed, and the turn the next attempt plans under."""

    model_config = ConfigDict(frozen=True)

    landed: bool
    turn: str | None


class _PlantedFinalize(BaseModel):
    """The ``version/create`` request that finalizes an attempt, and the digest of the manifest bytes it names."""

    model_config = ConfigDict(frozen=True)

    request: CreateTableVersionRequest
    judged: str


def _attempt_once[T](
    commit: _StampedCommit,
    dataset: lance.LanceDataset,
    plan: Callable[[lance.LanceDataset], tuple[Transaction, T]],
    *,
    turn: str | None,
    next_turn: Callable[[], str | None],
) -> _AttemptOutcome[T]:
    """Plan, judge and finalize one attempt: what it landed, and the turn the next attempt plans under."""
    try:
        transaction, measured = plan(dataset)
    except _PlannedOnAStaleVersion:
        return _AttemptOutcome[T](landed=None, turn=next_turn())
    attempt = _commit_detached(commit, dataset, transaction, turn=turn)
    wrote: file_identities.CommittedFiles | None = None
    recorded = finalizing = False
    try:
        _judge_attempt(commit, attempt)
        wrote = committed_files.added(
            dataset,
            attempt.detached,
            table_location=commit.table.location,
            branch=commit.ref,
            so=commit.so,
            transaction_uuid=attempt.stamped.uuid,
            read_version=attempt.stamped.read_version,
        )
        commit.table.record_committed_files(wrote)
        recorded = True
        planted = _write_the_finalize_manifest(commit, attempt)
        if commit.author is not None:
            _record_attempt_author(commit, attempt, planted, author=commit.author)
        finalizing = True
        finalized = _finalize_attempt(commit, attempt, planted, wrote=wrote, next_turn=next_turn)
        turn = finalized.turn
        if not finalized.landed:
            return _AttemptOutcome[T](landed=None, turn=turn)
    except BaseException:
        discard_detached_manifest(attempt.location, commit.so, attempt.finalize_id)
        if not finalizing:
            if wrote is None:
                wrote = _read_attempt_files(commit.table, commit.so, dataset, after=attempt.detached, transaction=attempt.stamped, branch=commit.ref)
            _discard_unlanded_attempt(commit.table, commit.so, attempt.stamped, wrote=wrote, branch=commit.ref, recorded=recorded)
        raise
    finally:
        discard_detached_manifest(attempt.location, commit.so, attempt.detached.version)
    return _AttemptOutcome[T](landed=(attempt.version, measured), turn=turn)


def _commit_detached(commit: _StampedCommit, dataset: lance.LanceDataset, transaction: Transaction, *, turn: str | None) -> _Attempt:
    """Commit ``transaction`` stamped with the commit's properties, detached, as the version after ``dataset``'s."""
    stamped = replace(transaction, read_version=dataset.version, transaction_properties={**(transaction.transaction_properties or {}), **commit.properties})
    detached = lance.LanceDataset.commit(dataset, stamped, detached=True)
    # The ref's own location: a branch handle reports the table's root, while its versions live under
    # `tree/<branch>/`, where the detached commit wrote (measured on pylance 13.0.0).
    location = detached.uri
    version = dataset.version + 1
    finalize_id = manifest_bytes.detached_id()
    return _Attempt(dataset=dataset, stamped=stamped, detached=detached, location=location, version=version, finalize_id=finalize_id, turn=turn)


def _judge_attempt(commit: _StampedCommit, attempt: _Attempt) -> None:
    """Refuse the attempt a judge refuses, as :func:`commit_stamped` states."""
    if commit.keeps_labels:
        _refuse_a_lost_label(attempt.dataset, attempt.detached)
    encoding.require_no_new_invalid_encoding(attempt.dataset, attempt.detached)
    primary_keys.refuse_repeats_in_version(attempt.detached, attempt.dataset, door=commit.properties[OPERATION_PROPERTY])
    contracts.require_kept(commit.table.contract(), attempt.dataset.schema, attempt.detached.schema, door=commit.properties[OPERATION_PROPERTY])


def _write_the_finalize_manifest(commit: _StampedCommit, attempt: _Attempt) -> _PlantedFinalize:
    """Write the detached manifest again as the attempt's version under its finalize id, and the request that finalizes it."""
    fs, base = dataset_fs(attempt.location, commit.so)
    versions = f"{base.rstrip('/')}/_versions"
    with fs.open_input_stream(f"{versions}/{manifest_names.detached_name(attempt.detached.version)}") as source:
        body = manifest_bytes.with_manifest_version(source.read(), attempt.version)
    with fs.open_output_stream(f"{versions}/{manifest_names.detached_name(attempt.finalize_id)}") as sink:
        sink.write(body)
    request = CreateTableVersionRequest(
        id=commit.table.segments,
        version=attempt.version,
        branch=commit.ref,
        manifest_path=f"{object_store_path(attempt.location)}/_versions/{manifest_names.detached_name(attempt.finalize_id)}",
        manifest_size=len(body),
    )
    return _PlantedFinalize(request=request, judged=commit_records.manifest_digest(body))


def _record_attempt_author(commit: _StampedCommit, attempt: _Attempt, planted: _PlantedFinalize, *, author: str) -> None:
    """Record the commit's author for the attempt's planted manifest."""
    # BEFORE THE FINALIZE: the reconciler signs a re-announcement only for the author recorded for exactly these bytes (ADR
    # 0187), which the backend moves into the version slot unchanged, under the ref's incarnation as this attempt reads it.
    ref_key = commit_ref_key(commit.table.location, commit.so, commit.table.segments, branch=commit.ref)
    commit.table.record_author(attempt.stamped.uuid, version=attempt.version, ref=ref_key, author=author, manifest=planted.judged)


def _finalize_attempt(
    commit: _StampedCommit, attempt: _Attempt, planted: _PlantedFinalize, *, wrote: file_identities.CommittedFiles | None, next_turn: Callable[[], str | None]
) -> _Finalized:
    """Finalize the planted attempt under its turn: whether it landed, and the turn the next attempt plans under."""
    try:
        commit.table.finalize_version(planted.request, turn=attempt.turn)
    except (table_claims.TableChangedError, table_claims.TurnWaitRefusedError):
        if commit.author is not None:
            _forget_unlanded_author(commit.table, commit.so, attempt.stamped.uuid, location=attempt.location, version=attempt.version, judged=planted.judged)
        raise
    except ConcurrentModificationError:
        if _discard_a_lost_finalize_unless_it_landed(commit, attempt, planted, wrote=wrote):
            # The store answered a conditional put that landed as a conflict: the version is this commit, and
            # planning it again would apply the change twice.
            return _Finalized(landed=True, turn=attempt.turn)
        return _Finalized(landed=False, turn=next_turn())
    return _Finalized(landed=True, turn=attempt.turn)


def _discard_a_lost_finalize_unless_it_landed(
    commit: _StampedCommit, attempt: _Attempt, planted: _PlantedFinalize, *, wrote: file_identities.CommittedFiles | None
) -> bool:
    """Whether a lost finalize landed after all; one that did not forgets its author and deletes what it wrote."""
    discard_detached_manifest(attempt.location, commit.so, attempt.finalize_id)
    landed = _landed_digest(commit.table, commit.so, location=attempt.location, version=attempt.version)
    if landed == planted.judged:
        return True
    if commit.author is not None:
        _forget_unlanded_author(commit.table, commit.so, attempt.stamped.uuid, location=attempt.location, version=attempt.version, judged=planted.judged)
    if landed != _AMBIGUOUS_VERSION:
        _discard_unlanded_attempt(commit.table, commit.so, attempt.stamped, wrote=wrote, branch=commit.ref, recorded=True)
    return False


def _discard_unlanded_attempt(
    table: table_claims.DoorTable,
    so: StorageOptions,
    transaction: Transaction,
    *,
    wrote: file_identities.CommittedFiles | None,
    branch: str | None,
    recorded: bool,
) -> None:
    """Delete what a commit attempt that never landed wrote ([[LH-564]]): its transaction file, and every file it added when
    its operation writes its own (:func:`committed_files.written_by`), then their committed-files record when ``recorded``.

    Called only once the attempt cannot land, its finalize lost to another commit's manifest or the attempt refused before
    its finalize, so no version names these files, and the next attempt plans afresh and writes its own. Lance would keep
    them 7 days, since a store cannot tell a dead commit's file from one still being written
    (``lance_docs/lance/guide/read_and_write.md:996-1003``); the catalog knows them for its own. It raises nothing, since it
    runs while another error is on its way: a delete the store refuses is logged and leaves its file to Lance's rule.
    """
    own = committed_files.written_by(transaction, wrote, branch=branch)
    root = committed_files.table_root(table.location)
    for path in own:
        delete_table_object(root, so, path)
    log.info("commit_attempt_discarded", extra={"table": table.segments, "transaction": transaction.uuid, "files": len(own)})
    if not recorded:
        return
    try:
        table.forget_committed_files(transaction.uuid)
    except (OSError, UnidentifiedObjectError) as exc:
        log.warning("committed_files_unforgotten", extra={"table": table.segments, "transaction": transaction.uuid, "error": str(exc)[:300]})


def _read_attempt_files(
    table: table_claims.DoorTable, so: StorageOptions, before: lance.LanceDataset, *, after: lance.LanceDataset, transaction: Transaction, branch: str | None
) -> file_identities.CommittedFiles | None:
    """What a refused attempt added, read on its way out; ``None`` when the store does not answer, which leaves the files it
    wrote but its transaction file to Lance's rule rather than replace the refusal."""
    try:
        return committed_files.added(
            before, after, table_location=table.location, branch=branch, so=so, transaction_uuid=transaction.uuid, read_version=transaction.read_version
        )
    except OSError as exc:
        log.warning("commit_attempt_files_unread", extra={"table": table.segments, "transaction": transaction.uuid, "error": str(exc)[:300]})
        return None


#: What :func:`committed_manifest_digest` answers for a version found under both naming schemes: it proves no commit's
#: manifest there, so an attempt that lost to it keeps what it wrote.
_AMBIGUOUS_VERSION: Final = ""


def _landed_digest(table: table_claims.DoorTable, so: StorageOptions, *, location: str, version: int) -> str | None:
    """The digest of the manifest ``version`` of the ref whose own location is ``location`` holds
    (:func:`committed_manifest_digest`): an attempt's own judged digest when its commit landed there, ``None`` for no
    manifest, ``""`` for an ambiguous version.

    Raises:
        ServiceUnavailableError: The store did not answer the read, so whether the commit landed is unknown: planning it
            again could apply it twice, and its author record is kept.
    """
    try:
        return committed_manifest_digest(location, so, version)
    except OSError as exc:
        log.warning("commit_landing_unread", extra={"table": table.segments, "version": version, "error": str(exc)[:300]})
        raise ServiceUnavailableError(f"the object store did not answer whether version {version} landed; describe the table before retrying") from exc


def _forget_unlanded_author(table: table_claims.DoorTable, so: StorageOptions, transaction_uuid: str, *, location: str, version: int, judged: str) -> None:
    """Delete the author an attempt recorded before a finalize that did not land, unless the version holds the very
    manifest it judged, which a store that did not answer may hide (ADR 0187). Every attempt plans a transaction of its
    own, so no other attempt relies on the record. A read or a delete the store refuses is logged and leaves it."""
    try:
        if committed_manifest_digest(location, so, version) == judged:
            return
        table.forget_author(transaction_uuid, version=version)
    except OSError as exc:
        log.warning("commit_author_unforgotten", extra={"table": table.segments, "transaction": transaction_uuid, "version": version, "error": str(exc)[:300]})


def committed_manifest_digest(location: str, so: StorageOptions, version: int) -> str | None:
    """The :func:`commit_records.manifest_digest` of the manifest committed as ``version`` on the ref whose own location is
    ``location`` (the table's root, or a branch's ``tree/<name>``); ``None`` when the ref holds no such version. A version
    found under both naming schemes is ambiguous and answers ``""``, which names no manifest: Lance opens one of them by
    the table's scheme, and a record must not be matched against the other.

    Raises:
        OSError: The store refused, or could not be reached for, the read.
    """
    fs, base = dataset_fs(location, so)
    found: list[str] = []
    for name in manifest_names.committed_names(version):
        try:
            with fs.open_input_stream(f"{base.rstrip('/')}/_versions/{name}") as source:
                found.append(commit_records.manifest_digest(source.read()))
        except FileNotFoundError:
            continue
    if not found:
        return None
    return found[0] if len(found) == 1 else ""


#: What Lance raises planning an expression it cannot resolve: an unknown column is a ``ValueError`` (pylance 13.0.0).
_UNPLANNED: Final = (ValueError, OSError, RuntimeError, pa.ArrowException)


def _plans(dataset: lance.LanceDataset, expression: str) -> bool:
    try:
        dataset.to_table(columns={"probe": expression}, limit=0)
    except _UNPLANNED:
        return False
    return True


def expressions_reading_a_label(dataset: Any, expressions: Sequence[str]) -> list[str]:  # noqa: ANN401 — a lance.LanceDataset
    """The ``expressions`` that read a classified column of this ref, each as given ([[LH-207]]).

    LANCE RESOLVES THE COLUMNS, not a reading of the SQL here: each expression is planned on an empty table of the ref's
    schema and on one with every top-level column holding a label left out (copying a struct copies the field under it).
    One that plans on the first and not the second reads a label; one that plans on neither is invalid, which the door's
    own call answers. Measured on pylance 13.0.0, quoting, backticks, non-ASCII names and a quote inside an identifier are
    each resolved as Lance resolves them when it runs the expression, which a reading of the SQL got wrong.

    Raises:
        OSError: The ref's schema could not be read.
    """
    labelled = {path.split(".", 1)[0] for path in classified_columns(dataset)}
    if not labelled or not expressions:
        return []
    schema = dataset.schema
    with tempfile.TemporaryDirectory() as scratch:
        # Two empty probe tables in a scratch directory the call removes: no table of the estate is written.
        full = lance.write_dataset(schema.empty_table(), f"{scratch}/full")  # noqa: TID251
        unlabelled = pa.schema([field for field in schema if field.name not in labelled])
        kept = lance.write_dataset(unlabelled.empty_table(), f"{scratch}/kept")  # noqa: TID251
        return [expression for expression in expressions if _plans(full, expression) and not _plans(kept, expression)]


def _refuse_a_lost_label(before: lance.LanceDataset, after: lance.LanceDataset) -> None:
    """Refuse a commit that loses or changes any field's ``rask.*`` metadata, by Lance field id ([[LH-207]]).

    Raises:
        InvalidInputError: A labelled field is gone, or its ``rask.*`` keys differ.
    """
    kept = labelled_fields(after)
    lost = sorted(path for field_id, (path, governed) in labelled_fields(before).items() if kept.get(field_id, ("", {}))[1] != governed)
    if lost:
        raise InvalidInputError(
            f"this would remove the label on {', '.join(lost)}: a label leaves a field only through update_field_metadata, which a classifier does"
        )


class _StagedCapture(Exception):
    """Raised by :class:`_CapturingNamespace` in place of a finalize, ending the operation that staged its manifest."""


class _PlannedOnAStaleVersion(Exception):
    """A captured operation met a version committed after the one it was planned on: :func:`commit_stamped` plans again.

    Lance checks the latest version before it stages (pylance 13.0.0): an operation that conflicts with one committed
    since is refused there as a retryable conflict, and one that does not is rebased and staged as a later version.
    """


class _CapturingNamespace(LanceNamespace):
    """The namespace a handle is opened through so that Lance's own operation stages its commit and finalizes none.

    Under managed versioning (``lance_docs/ns_catalog/spec.yaml`` CreateTableVersion) a handle's commit writes the version
    as a staged manifest and hands its path to the namespace's ``create_table_version``; this answers that call by
    keeping the request and raising, so the operation's transaction, planned and written by Lance itself, can be read
    off the staged manifest and committed with the door's stamp (:func:`captured`). Measured on pylance 13.0.0: update,
    delete, add_columns, alter_columns (rename and nullability), drop_columns, both metadata updates and drop_index each
    stage one manifest this way, on main and on a branch, and the transaction re-committed with transaction properties
    leaves the rows, schema, indices and version Lance's own call leaves.

    The ref's versions are answered off its own location, as the ``dir`` backend answers them (a ``TableVersion`` naming
    the V2 manifest's object-store path), with the version planned on as the latest and none after it, so Lance stages
    exactly the next version and the finalize's put-if-not-exists decides the race ([[LH-259]]). Measured on pylance 13.0.0
    with a writer appending through the catalog's ``/insert`` in a tight loop: answered as found, a version committed since
    cost a delete, an update and a drop Lance's own retries on every plan, 214-302 s before :func:`commit_stamped` answered
    409; answered as absent, each plan lost within a second. Lance can still find such a version on the store itself, which
    :class:`_PlannedOnAStaleVersion` covers. Nothing here names the table, so nothing resolves its name again ([[LH-530]]).

    THIS PATH IS rask's ONLY BECAUSE THESE OPERATIONS TAKE NO TRANSACTION PROPERTIES. pylance 13.0.0 takes them on
    ``write_dataset``, ``insert`` and ``LanceDataset.commit`` alone; lance-format/lance#9888 (merged 2026-10-24) passes
    ``WriteParams`` to merge insert and states that ``transaction_properties`` do not apply there, and a search of
    lance-format/lance on 2026-10-30 found no issue asking for them on the operations above. Once Lance takes them, these doors commit through Lance's own call, which
    retries a lost race itself, and this namespace and its re-planning ([[LH-542]]) go.
    """

    def __init__(self, table: table_claims.DoorTable, so: StorageOptions, *, ref: str | None, planned: int) -> None:
        self._table = table
        self._so = so
        self._ref = ref
        self._planned = planned
        self.staged: dict[str, Any] | None = None

    def ref_location(self, branch: str | None) -> str:
        """Where ``branch``'s versions live: the table's root for main, else its ``tree/<branch>``."""
        root = self._table.location.rstrip("/")
        return root if branch is None else f"{root}/{branch_layout.BRANCH_CONTAINER}/{branch}"

    def _latest(self, branch: str | None) -> int:
        """The version planned on for the ref planned on; any other ref's own latest (pylance opens main first)."""
        if branch == self._ref:
            return self._planned
        return int(open_dataset(self.ref_location(branch), storage_options=self._so.in_clear() or None, session=shared_lance_session()).version)

    def namespace_id(self) -> str:
        return "rask-stamped-commit"

    def describe_table(self, request: DescribeTableRequest) -> DescribeTableResponse:
        return DescribeTableResponse(location=self._table.location, managed_versioning=True, storage_options=self._so.in_clear() or None)

    def _version(self, branch: str | None, version: int) -> TableVersion:
        location = self.ref_location(branch)
        fs, base = dataset_fs(location, self._so)
        # V2 names first, as every table the catalog creates is named; a table written with V1 paths names `<n>.manifest`.
        for name in manifest_names.committed_names(version):
            info = fs.get_file_info(f"{base.rstrip('/')}/_versions/{name}")
            if info.type == pafs.FileType.File:
                break
        else:
            raise TableVersionNotFoundError(f"version {version} is not on the ref")
        when = info.mtime_ns // 1_000_000 if info.mtime_ns is not None else None
        return TableVersion(version=version, manifest_path=f"{object_store_path(location)}/_versions/{name}", manifest_size=info.size, timestamp_millis=when)

    def list_table_versions(self, request: ListTableVersionsRequest) -> ListTableVersionsResponse:
        branch = recorded_branch(dict(request).get("branch"))
        return ListTableVersionsResponse(versions=[self._version(branch, self._latest(branch))])

    def describe_table_version(self, request: DescribeTableVersionRequest) -> DescribeTableVersionResponse:
        asked = dict(request)
        branch = recorded_branch(asked.get("branch"))
        version = int(asked["version"]) if asked.get("version") is not None else self._latest(branch)
        if branch == self._ref and version > self._planned:
            raise TableVersionNotFoundError(f"version {version} is not on the ref")
        return DescribeTableVersionResponse(version=self._version(branch, version))

    def create_table_version(self, request: CreateTableVersionRequest) -> CreateTableVersionResponse:
        self.staged = dict(request)
        raise _StagedCapture

    def moved(self) -> bool:
        """Whether the ref holds a version after the one planned on: another writer committed since the plan.

        Asked of the store, never of Lance's wording: Lance learns of the version only by probing it here, so it either
        stages over it, proves that staging lost and deletes it, or exhausts its own retries against the planned version
        this namespace keeps answering as the latest ([[LH-542]]).
        """
        try:
            self._version(self._ref, self._planned + 1)
        except TableVersionNotFoundError:
            return False
        return True


def captured[T](
    table: table_claims.DoorTable,
    so: StorageOptions,
    operation: Callable[[lance.LanceDataset], object],
    *,
    branch: str | None,
    measure: Callable[[lance.LanceDataset], T] | None = None,
) -> Callable[[lance.LanceDataset], tuple[Transaction, T | None]]:
    """A :func:`commit_stamped` plan running Lance's own ``operation`` on the version planned on, and taking the
    transaction it stages (:class:`_CapturingNamespace`) without committing it. ``branch`` is the ref
    :func:`commit_stamped` plans on, which a handle does not report.

    ``measure`` reads what the door answers off the same version before the operation runs (an update's matched rows),
    since the operation ends in the capture and returns nothing. A failure of the operation before it staged anything
    (a predicate Lance refuses, a missing column) is raised as Lance raised it.
    """

    def plan(dataset: lance.LanceDataset) -> tuple[Transaction, T | None]:
        ref = recorded_branch(branch)
        namespace = _CapturingNamespace(table, so, ref=ref, planned=int(dataset.version))
        handle = open_dataset(namespace_client=namespace, table_id=table.segments, session=shared_lance_session())
        handle = handle.checkout_version((ref, dataset.version) if ref is not None else dataset.version)
        measured = measure(handle) if measure is not None else None
        try:
            operation(handle)
        except Exception as exc:
            if namespace.staged is None:
                # Lance's own answer for it, never the broad conflict markers: a column a caller named `concurrent` is
                # missing, not raced.
                if is_retryable_conflict(exc):
                    raise _PlannedOnAStaleVersion from exc
                # Lance gave up against the planned version, which this namespace keeps answering as the latest, while
                # another writer's version stood after it ([[LH-542]]): measured on pylance 13.0.0, a delete exhausts its
                # own retries ("Too many concurrent writers"). Conflict-shaped and found on the store, so a refusal of the
                # caller's own keeps its answer however busy the table is.
                if classify_commit_failure(exc) is CommitVerdict.RETRYABLE_CONFLICT and namespace.moved():
                    raise _PlannedOnAStaleVersion from exc
                raise
            if not isinstance(exc, (_StagedCapture, OSError, RuntimeError)):
                raise
        if namespace.staged is None:
            raise InternalError("the operation committed no staged version; its handle was not opened under managed versioning")
        try:
            transaction = _staged_transaction(namespace.ref_location(ref), so, str(namespace.staged["manifest_path"]))
        except FileNotFoundError as exc:
            # Lance deletes a manifest it staged once it proves that version another writer's (`lose_or_retain`,
            # `rust/lance-table/src/io/commit/external_manifest.rs` at v13.0.0); 1 race in 20 of six `add_columns` met it.
            if namespace.moved():
                raise _PlannedOnAStaleVersion from exc
            raise
        if namespace.staged.get("version") != dataset.version + 1:
            raise _PlannedOnAStaleVersion
        return transaction, measured

    return plan


def _staged_transaction(location: str, so: StorageOptions, manifest_path: str) -> Transaction:
    """The transaction of the manifest Lance staged at ``manifest_path`` under the ref at ``location``, read by planting a
    copy as a detached version; the staged manifest and the copy are both deleted.

    Raises:
        InternalError: The staged manifest names no transaction pylance can read.
    """
    name = manifest_path.rsplit("/", 1)[-1]
    fs, base = dataset_fs(location, so)
    versions = f"{base.rstrip('/')}/_versions"
    detached = manifest_bytes.detached_id()
    try:
        with fs.open_input_stream(f"{versions}/{name}") as source:
            body = manifest_bytes.with_manifest_version(source.read(), detached)
        with fs.open_output_stream(f"{versions}/{manifest_names.detached_name(detached)}") as sink:
            sink.write(body)
        transaction = read_transaction(
            open_dataset(location, version=detached, storage_options=so.in_clear() or None, session=shared_lance_session()), detached
        )
    finally:
        discard_detached_manifest(location, so, detached)
        delete_table_object(location, so, f"_versions/{name}")
    if transaction is None:
        raise InternalError("the staged manifest carries no transaction")
    return transaction
