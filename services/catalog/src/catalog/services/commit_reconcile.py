"""Find every committed version whose announcement is not on lineage's feed: the commit is the outbox ([[LH-497]]).

The catalog announces a write after its commit, by staging the event in its object-store outbox and publishing it, and a
crash between the commit and the stage loses the announcement. The version itself is not lost: the catalog recorded the
caller it authorized every commit for (:mod:`service_kit.lakehouse.commit_records`, ADR 0187), a catalog door's commit
names its lineage operation (``rask.operation``), and an announcement's run id is derived from the commit itself
(:mod:`catalog.core.commit_ids`). So each table's own version log is the outbox: a pass reads each ref's versions after
its mark, looks for each on the feed, and hands the caller every version the feed lacks, rebuilt from the version log and
signed for the author recorded, never for the ``rask.author`` a version carries, which any writer that can put a final
manifest can stamp. A version the catalog recorded no author for was committed past every door, and is counted.

ON THE FEED means a row naming the table's uuid, the ref and its incarnation, and the version: name-independent, so a
table renamed since its write is not announced again under its new name. The rows are read off ``provenance$events``
in-process, as the catalog reads any table it governs, bounded below by the oldest commit the pass looks for.

A version is looked for only once it is older than the grace (``LANCE_COMMIT_RECONCILE_GRACE_SECONDS``): the inline
publish and the outbox relay deliver a commit's own announcement well within it, and announcing one they are still
delivering would only be a duplicate, which the derived run id lands once anyway.

A REGISTERED table is looked at only after its register ([[LH-550]]). The versions a register attaches were committed
past every door, so the catalog announced none of them and vouches for no ``rask.author`` they carry; the register's own
``register_table`` marker is their announcement. The register door marks each ref at the head it found
(:func:`mark_registered_heads`) before the table's claim makes it one a pass reads.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any, Final

import lance
from lance_namespace import InvalidInputError, ServiceUnavailableError
from pydantic import BaseModel, ConfigDict

from catalog.core import lineage_emit, run_context
from catalog.core.config import Settings
from catalog.services import dataplane, managed_versions, provenance_namespace
from service_kit.governed import fga
from service_kit.lakehouse import commit_records
from service_kit.lakehouse.features import flags_from_open_error
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lancekit.filters import sql_literal
from service_kit.openlineage import BRANCH_IDENTIFIER, LANCE_FACET, dataset_uuids


log = logging.getLogger(__name__)

#: Lance operations a version can carry that no door announces: a branch's first version (Lance's own ``create_branch``
#: commits a ``Clone``), and a commit changing only the manifest's config (the doors' and the sweep's disarm of
#: commit-path cleanup, [[LH-245]]).
_UNANNOUNCED: Final = frozenset({"Clone"})


class Commit(BaseModel):
    """One committed version whose announcement the feed lacks, as its version log records it."""

    model_config = ConfigDict(frozen=True)

    version: int
    branch: str | None
    branch_identifier: str | None
    operation: str
    #: The author the catalog recorded authorizing it (ADR 0187); ``None`` where it recorded nobody: a commit made past
    #: every door, or one where the catalog authenticates nobody.
    author: str | None = None
    #: The commit's ``rask.*`` run, when it names one.
    run: run_context.RunContext | None
    committed_at: str | None
    #: Whether a table's first version committed rows, which makes its create a write ([[LH-492]]).
    commits_rows: bool
    transaction_uuid: str


class RefScan(BaseModel):
    """What a pass found on one ref of one table."""

    model_config = ConfigDict(frozen=True)

    branch: str | None
    branch_identifier: str | None
    #: The versions to announce, oldest first.
    missing: tuple[Commit, ...] = ()
    #: Versions of an operation the doors announce for which the catalog recorded no author: committed past every door, so
    #: unannounceable.
    unauthored: tuple[int, ...] = ()
    #: The commits whose recorded author is no longer needed: their versions are on the feed, past the horizon, or counted
    #: unauthored.
    forget: tuple[Commit, ...] = ()
    #: The highest version up to which every version is on the feed, unannounced by design, or unauthored.
    mark: int | None = None


def announced_commits(feed_location: str, so: StorageOptions, table_uuid: str, *, since: datetime) -> set[tuple[str, int]]:
    """The ``(ref key, version)`` of every commit of ``table_uuid`` the feed holds a row for, received at ``since`` or after.

    The ref key is :func:`commit_records.ref_key`'s. A row naming the table among its outputs (a run) or as its dataset
    (a static change) at a version counts, whatever the name it used.
    """
    feed = lance.dataset(feed_location, storage_options=so.in_clear() or None)
    rows = feed.to_table(
        columns=["event"],
        filter=f"array_has(table_uuids, {sql_literal(table_uuid)}) AND received_at >= TIMESTAMP {sql_literal(since.strftime('%Y-%m-%d %H:%M:%S'))}",
    )
    found: set[tuple[str, int]] = set()
    for text in rows.column("event").to_pylist():
        try:
            event = json.loads(text)
        except (TypeError, ValueError):
            continue
        found |= set(_commits_named(event, table_uuid))
    return found


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _commits_named(event: dict[str, Any], table_uuid: str) -> Iterator[tuple[str, int]]:
    run_lance = _mapping(_mapping(_mapping(event.get("run")).get("facets")).get(LANCE_FACET))
    outputs = event.get("outputs")
    datasets: list[object] = [*outputs] if isinstance(outputs, list) else []
    if isinstance(event.get("dataset"), dict):
        datasets.append(event["dataset"])
    for dataset in datasets:
        if not isinstance(dataset, dict) or dataset_uuids(dataset).table_uuid != table_uuid:
            continue
        facets = _mapping(dataset.get("facets"))
        named = {**run_lance, **_mapping(facets.get(LANCE_FACET))}
        if named.get("operation") in lineage_emit.TAG_MOVES:
            # A tag move names a version another write committed; it is no announcement of that commit.
            continue
        raw = _mapping(facets.get("version")).get("datasetVersion", named.get("version"))
        try:
            version = int(str(raw))
        except ValueError:
            continue
        ref = named.get("ref") if isinstance(named.get("ref"), str) else None
        identifier = named.get(BRANCH_IDENTIFIER) if isinstance(named.get(BRANCH_IDENTIFIER), str) else None
        yield commit_records.ref_key(ref, identifier), version


def refs_of(location: str, so: StorageOptions) -> list[tuple[str | None, str | None]]:
    """Main and every branch of the table, each with its incarnation (pylance's ``branch_identifier``)."""
    return _listed_refs(lance.dataset(location, storage_options=so.in_clear() or None))


def _listed_refs(dataset: lance.LanceDataset) -> list[tuple[str | None, str | None]]:
    """Main, then each branch ``dataset`` lists, with the incarnation the same listing records: the one key a pass and a
    register's marks both read a ref by."""
    listed = dataset.branches.list() or {}
    return [(None, None), *((name, dataplane.identifier_of(listed[name])) for name in sorted(listed))]


def mark_registered_heads(settings: Settings, so: StorageOptions, *, location: str, table_uuid: str) -> None:
    """Mark every ref of the dataset at ``location`` at its head, under ``table_uuid``: a pass then looks only for the
    commits made after the register that attaches it ([[LH-550]]).

    Each ref is keyed as :func:`reconcile_table` keys it, main and each branch by its incarnation. A location holding no
    committed version has no head to mark, and a pass finds nothing there until a commit lands.

    Raises:
        ServiceUnavailableError: A ref's head could not be read or the mark could not be written; no mark was written.
    """
    try:
        heads = _registered_heads(location, so)
        if heads:
            commit_records.write_marks(settings.registry_root, settings.storage_options(), table_uuid, commit_records.CommitMarks(refs=heads))
    except (OSError, ValueError) as exc:
        log.warning("registered_heads_unmarked", extra={"location": location, "error": str(exc)[:300]})
        raise ServiceUnavailableError(f"the heads of the dataset at {location!r} could not be read and marked, so it was not registered") from exc


def _registered_heads(location: str, so: StorageOptions) -> dict[str, int]:
    """Each ref's head version, keyed as a mark keys it.

    None when the location holds no committed version, or when pylance refuses to open the dataset over its feature
    flags: the register's judge refuses such a dataset, 400 for mixed data file versions and 503 for any other flag
    (`table_bases.judge_registered_table`).
    """
    if not dataplane.holds_a_version(location, so):
        return {}
    try:
        dataset = lance.dataset(location, storage_options=so.in_clear() or None)
    except (OSError, ValueError) as exc:
        if flags_from_open_error(exc) is not None:
            return {}
        raise
    return {
        commit_records.ref_key(branch, identifier): (dataset if branch is None else dataset.checkout_version((branch, None))).version
        for branch, identifier in _listed_refs(dataset)
    }


class ReconcileReadError(Exception):
    """A ref's version log could not be read past a point: the pass stops there and keeps the mark it had."""


def scan_ref(
    location: str,
    so: StorageOptions,
    *,
    branch: str | None,
    branch_identifier: str | None,
    mark: int | None,
    announced: set[tuple[str, int]],
    after: datetime,
    before: datetime,
    record_of: Callable[[str, int, str], commit_records.CommitAuthor | None],
    authenticates: bool,
    unannounced: Callable[[str], bool],
) -> RefScan:
    """Every version of ``branch`` after ``mark``, committed between ``after`` and ``before``, that the feed (``announced``)
    lacks.

    A version on the feed is confirmed, whoever made it. ``after`` is the horizon: a version committed before it is past
    the feed's own retention, so the feed may hold no row for it and the pass confirms it unread. ``record_of`` answers,
    for a transaction's uuid, the version it holds and the ref's key, the record of the author the catalog authorized that
    commit there for, the only author a re-announcement is signed for (ADR 0187), and only when the version holds the
    manifest the record names, if it names one: a version's own ``rask.author`` is whatever its writer stamped, and its
    transaction's uuid is one every reader of the table sees. ``authenticates`` is whether the
    catalog authenticates its callers: where it does, a missing version the catalog recorded no author for was committed
    past every door, and is counted rather than announced, since an announcement must name who made it; so is a version
    whose transaction cannot be read, or whose operation no door commits. ``unannounced`` answers, for an operation,
    whether the emitter leaves it unannounced on this table (a row write in the provenance namespace,
    `catalog.services.provenance_namespace`), which the feed then holds no row for and the catalog keeps no author for.

    Raises:
        ReconcileReadError: A retained version, or its commit time, could not be read; the mark stops before it.
        OSError: A recorded author, or the manifest it names, could not be read.
    """
    key = commit_records.ref_key(branch, branch_identifier)
    ref_location = managed_versions.CommitRef(table_location=location, branch=branch).location
    missing: list[Commit] = []
    unauthored: list[int] = []
    forget: list[Commit] = []
    confirmed = mark
    contiguous = True
    for version, handle in _versions_after(location, so, branch, mark):
        when = _committed_at(handle)
        if when is None:
            raise ReconcileReadError(f"version {version} of {location} on {branch or 'main'} records no readable commit time")
        if when >= before:
            break
        on_feed = when < after or (key, version) in announced
        try:
            commit = _commit_at(handle, version, branch=branch, branch_identifier=branch_identifier)
        except UnidentifiedCommitError as exc:
            if not on_feed and authenticates:
                log.warning("commit_reconcile_unidentified", extra={"location": location, "branch": branch, "version": version, "error": str(exc)})
                unauthored.append(version)
            commit = None
        if commit is None or unannounced(commit.operation):
            pass
        elif on_feed:
            forget.append(commit)
        else:
            record = record_of(commit.transaction_uuid, version, key)
            author = _signed_for(record, ref_location, so, version)
            if author is None and authenticates:
                unauthored.append(version)
                if record is not None:
                    # This ref's record for other bytes signs nothing, and is not needed once the version is counted.
                    forget.append(commit)
            else:
                missing.append(commit.model_copy(update={"author": author}))
                contiguous = False
        if contiguous:
            confirmed = version
    return RefScan(
        branch=branch,
        branch_identifier=branch_identifier,
        missing=tuple(missing),
        unauthored=tuple(unauthored),
        forget=tuple(forget),
        mark=confirmed,
    )


def _signed_for(record: commit_records.CommitAuthor | None, ref_location: str, so: StorageOptions, version: int) -> str | None:
    """The author a re-announcement of ``version`` is signed for: the one ``record`` names, when the record was read off the
    landed version or names the manifest committed there; ``None`` otherwise, a version put past every door under another
    commit's transaction uuid included.

    Raises:
        OSError: The manifest committed as ``version`` could not be read.
    """
    if record is None:
        return None
    if record.manifest is not None and record.manifest != dataplane.committed_manifest_digest(ref_location, so, version):
        return None
    return record.author


def _versions_after(location: str, so: StorageOptions, branch: str | None, mark: int | None) -> Iterator[tuple[int, lance.LanceDataset]]:
    """Each retained version of ``branch`` after ``mark``, oldest first, opened.

    The retained versions are the ref's own listing: a version cleanup reclaimed is not in it, so it is skipped, and a
    retained version that fails to open stops the pass rather than being taken for reclaimed.

    Raises:
        ReconcileReadError: A retained version could not be opened.
    """
    root = lance.dataset(location, storage_options=so.in_clear() or None)
    latest = root.checkout_version((branch, None)) if branch is not None else root
    retained = sorted(int(entry["version"]) for entry in latest.versions())
    for version in (v for v in retained if mark is None or v > mark):
        try:
            handle = latest.checkout_version((branch, version) if branch is not None else version)
        except (OSError, ValueError) as exc:
            raise ReconcileReadError(f"retained version {version} of {location} on {branch or 'main'} could not be opened: {exc}") from exc
        yield version, handle


def _committed_at(handle: lance.LanceDataset) -> datetime | None:
    stamp = dataplane.commit_time(handle)
    return datetime.fromisoformat(stamp) if stamp is not None else None


class UnidentifiedCommitError(Exception):
    """A version whose transaction cannot be read, or names an operation no door commits: none of the catalog's."""


def _commit_at(handle: lance.LanceDataset, version: int, *, branch: str | None, branch_identifier: str | None) -> Commit | None:
    """The commit at ``version`` as the doors announce it, or ``None`` for one no door announces.

    Raises:
        UnidentifiedCommitError: The version names no transaction, or one whose operation no door commits.
    """
    transaction = handle.read_transaction(version)
    if transaction is None:
        raise UnidentifiedCommitError(f"version {version} names no transaction")
    properties = dict(transaction.transaction_properties or {})
    name = type(transaction.operation).__name__
    if name in _UNANNOUNCED or _only_changes_config(transaction.operation):
        return None
    operation = properties.get(run_context.OPERATION_PROPERTY) or _operation_of(name, version=version, branch=branch)
    if operation is None:
        raise UnidentifiedCommitError(f"version {version} commits a {name}, which no door commits")
    stock = {key: value for key, value in properties.items() if key != run_context.OPERATION_PROPERTY}
    try:
        run = run_context.from_properties(stock)
    except InvalidInputError:
        # A malformed run is announced without one, never dropped.
        run = None
    fragments = getattr(transaction.operation, "fragments", None) or []
    return Commit(
        version=version,
        branch=branch,
        branch_identifier=branch_identifier,
        operation=operation,
        run=run,
        committed_at=dataplane.commit_time(handle),
        commits_rows=operation == lineage_emit.CREATE_TABLE and sum(int(fragment.physical_rows or 0) for fragment in fragments) > 0,
        transaction_uuid=transaction.uuid,
    )


def _operation_of(name: str, *, version: int, branch: str | None) -> str | None:
    """The lineage operation ``version/create`` announces a stock commit of Lance operation ``name`` as."""
    if branch is None and version == 1:
        return lineage_emit.CREATE_TABLE
    if name == lance.LanceOperation.BaseOperation.__name__:
        # The one operation pylance models no class for that `version/create` admits ([[LH-471]]).
        return lineage_emit.COMPACT_TABLE
    return managed_versions.LINEAGE_OPERATION.get(name)


def _only_changes_config(operation: object) -> bool:
    """Whether an ``UpdateConfig`` changes the manifest's config alone: a disarm of commit-path cleanup, announced by no door."""
    if type(operation).__name__ != "UpdateConfig":
        return False
    return not any(getattr(operation, field, None) for field in ("table_metadata_updates", "schema_metadata_updates", "field_metadata_updates"))


def grace_bound(now: datetime, grace_seconds: int) -> datetime:
    """The newest commit time a pass looks for: ``grace_seconds`` before ``now``."""
    return now - timedelta(seconds=grace_seconds)


class TableReconcile(BaseModel):
    """What a pass found on one table: each ref it read, and the refs it could not read past a point."""

    model_config = ConfigDict(frozen=True)

    scans: tuple[RefScan, ...] = ()
    unreadable: tuple[str, ...] = ()


#: How much earlier than the horizon the feed is read: a row is received after its commit, never before, but the store's
#: clock and the catalog's may disagree.
_FEED_SLACK: Final = timedelta(minutes=10)


def reconcile_table(
    settings: Settings,
    so: StorageOptions,
    *,
    segments: list[str],
    location: str,
    table_uuid: str,
    feed: str,
    now: datetime,
) -> TableReconcile:
    """Scan every ref of the table at ``location`` against the feed at ``feed``, forget the recorded author of every commit
    the scan confirmed, and keep each mark it confirmed.

    The window is the grace (``LANCE_COMMIT_RECONCILE_GRACE_SECONDS``) at the young end and the horizon
    (``LANCE_COMMIT_RECONCILE_HORIZON_SECONDS``, inside the feed's retention) at the old end. A ref whose log cannot be
    read keeps the mark it had and is named in ``unreadable``. A table only declared has no ref to read.
    """
    if not dataplane.holds_a_version(location, so):
        # A table only declared holds no version, so nothing of it was announced or lost.
        return TableReconcile()
    control, control_so = settings.registry_root, settings.storage_options()
    marks = commit_records.read_marks(control, control_so, table_uuid)
    after = now - timedelta(seconds=settings.commit_reconcile_horizon_seconds)
    announced = announced_commits(feed, so, table_uuid, since=after - _FEED_SLACK)
    parent = fga.parent_namespace_id(segments) or ""
    scans: list[RefScan] = []
    unreadable: list[str] = []
    for branch, identifier in refs_of(location, so):
        try:
            scans.append(
                scan_ref(
                    location,
                    so,
                    branch=branch,
                    branch_identifier=identifier,
                    mark=marks.refs.get(commit_records.ref_key(branch, identifier)),
                    announced=announced,
                    after=after,
                    before=grace_bound(now, settings.commit_reconcile_grace_seconds),
                    record_of=lambda transaction_uuid, version, ref: commit_records.recorded(
                        control, control_so, table_uuid, transaction_uuid=transaction_uuid, version=version, ref=ref
                    ),
                    authenticates=settings.oidc_enabled,
                    unannounced=lambda operation: provenance_namespace.is_unannounced(parent, operation, settings),
                )
            )
        except ReconcileReadError as exc:
            log.error("commit_reconcile_ref_unreadable", extra={"table": segments, "branch": branch, "error": str(exc)[:300]})
            unreadable.append(commit_records.ref_key(branch, identifier))
    kept: dict[str, int] = {}
    for scan in scans:
        mark = _forgotten_up_to(scan, control, control_so, table_uuid)
        if mark is not None:
            kept[commit_records.ref_key(scan.branch, scan.branch_identifier)] = mark
    if any(marks.refs.get(key) != value for key, value in kept.items()):
        commit_records.write_marks(control, control_so, table_uuid, commit_records.CommitMarks(refs={**marks.refs, **kept}))
    return TableReconcile(scans=tuple(scans), unreadable=tuple(unreadable))


def _forgotten_up_to(scan: RefScan, control: str, control_so: StorageOptions, table_uuid: str) -> int | None:
    """Forget the recorded author of each commit ``scan`` confirmed on the feed; answer the ref's mark, held below the
    first version whose record the store refused to delete, so the next pass reads that version and forgets it then."""
    mark = scan.mark
    for commit in scan.forget:
        try:
            commit_records.forget_author(control, control_so, table_uuid, transaction_uuid=commit.transaction_uuid, version=commit.version)
        except OSError as exc:
            log.warning("commit_author_unforgotten", extra={"table": table_uuid, "version": commit.version, "branch": scan.branch, "error": str(exc)[:300]})
            if mark is not None and commit.version <= mark:
                mark = commit.version - 1
    return mark
