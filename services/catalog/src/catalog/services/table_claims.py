"""The catalog's half of the location claims ([[LH-204]]): who may take a location, when a holder is gone, and the uuid a new table's claim records.

:mod:`lakehouse_kit.location_claims` is the store-arbitrated record; this module is what only the
catalog can answer about it — whether a claim's holder still resolves to the location — and the one
shape every door calls it in. A table's uuid is recorded here when a door brings the table into being (ADR 0173
§2): :func:`claim_new_table` for create, declare and register, :func:`take` for a table that already exists and
keeps its uuid.

A door resolves its table once (ADR 0173 §7, [[LH-521]]): :func:`resolve` reads the table's location and that
location's claim when the request reaches the door, and a drop, deregister or rename acts on that reading and
nothing else. A drop or deregister fences the claim it read (:func:`fence`) before its backend call and lifts the fence
once it has acted (:func:`lift_fence`); a rename hands over the claim it read (:func:`hand_over`). Every other door
acts on that reading through :class:`DoorTable` ([[LH-530]]).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import lance
from lance_namespace import (
    ConcurrentModificationError,
    CreateTableVersionRequest,
    DescribeTableRequest,
    InvalidTableStateError,
    LanceNamespace,
    NamespaceNotFoundError,
    ServiceUnavailableError,
    TableNotFoundError,
)
from pydantic import BaseModel

from catalog.core.config import Settings
from catalog.core.namespace import open_resolved_dataset, open_resolved_dataset_unchecked
from catalog.services import native
from catalog.services.object_uuids import UnidentifiedObjectError
from lakehouse_kit import commit_records, commit_turns, file_identities, history_fences, location_claims, trash
from lakehouse_kit.base_refs import decoded_path
from lakehouse_kit.location_claims import ClaimRead, ClaimStore, LocationClaim
from lakehouse_kit.version_retention import recorded_branch
from service_kit.governed import fga
from service_kit.storage_options import StorageOptions


log = logging.getLogger(__name__)


class ResolvedTable(BaseModel):
    """The table a request's name named when the request reached its door: the one the door acts on.

    The table's uuid is its claim's (ADR 0173 §6). No location means the name named no table.
    """

    segments: list[str]
    location: str | None = None
    #: The location's claim as the resolution read it, with its ETag; ``None`` when the location carries no record.
    claim: ClaimRead | None = None
    #: The name named no table because its namespace does not exist, as the backend answered (code 1, not 4).
    namespace_absent: bool = False

    @property
    def uuid(self) -> str | None:
        """The table's uuid as its claim recorded it when the request reached the door."""
        return self.claim.claim.uuid if self.claim is not None else None

    def require_uuid(self) -> str:
        """The table's uuid, for a door that reads or writes a record kept for it (ADR 0173 §8).

        Raises:
            TableNotFoundError: The name named no table.
            UnidentifiedObjectError: The table's location carries no claim recording a uuid, which no door leaves.
        """
        if self.location is None:
            raise TableNotFoundError(f"table not found: {self.segments}")
        return _recorded_uuid(self.claim, self.segments, self.location)


class DoorFence(BaseModel):
    """The fence a door set on its table's claim: the location it acts on and the request token the fence carries."""

    location: str
    token: str


def claim_store(settings: Settings) -> ClaimStore:
    """The claims live on the control root beside the trash and base records, written with its credential."""
    return ClaimStore(control_root=settings.registry_root, storage_options=settings.storage_options())


def resolve(ns: LanceNamespace, store: ClaimStore, segments: list[str]) -> ResolvedTable:
    """Resolve the table ``segments`` names to its location and that location's claim, once.

    A declared table resolves too; whether it holds a version is ``dataplane.holds_a_version``'s to judge where a door
    asks, never the backend's verdict ([[LH-528]]). A name that names no table, in a namespace that exists or one that does
    not (the ``dir`` backend answers ``NamespaceNotFoundError`` for that, measured on pylance 13.0.0), resolves to no
    location rather than raising, so a door answers for that itself after the checks it makes first, an idempotent
    replay of a request that already ended the table among them.
    """
    if not segments:
        return ResolvedTable(segments=[])
    try:
        described = native.call(ns, "describe_table", DescribeTableRequest(id=segments))
    except TableNotFoundError:
        return ResolvedTable(segments=segments)
    except NamespaceNotFoundError:
        return ResolvedTable(segments=segments, namespace_absent=True)
    location = described.location or None
    return ResolvedTable(segments=segments, location=location, claim=location_claims.read(store, location) if location else None)


type _Open = Callable[..., lance.LanceDataset]


class DoorTable:
    """The table a door acts on: what its name resolved to when the request reached the door (ADR 0173 §7, [[LH-530]]).

    The router authorized the uuid this resolution read, and the door acts at the location it read, never at the name:
    every handle it reads or commits through opens there. Each open asks first whether that is still the table authorized
    (:meth:`require_held`): the location's claim is the one the resolution read, under its ETag, and the table it records
    is not in the trash. A drop, a re-create, a rename, a deregister and a re-register each write the claim; a
    recoverable drop keeps it as it was, uuid and all, and files the trash record instead, which an undrop whose last
    step failed leaves beside the restored table, so a trash record is settled by whether the name still resolves there.
    Either way the door answers 409, and acts on no other table and on none in the trash.

    A call to the native backend takes the name and no location, so around one the door asks whether the name still
    resolves to the location under that claim (:meth:`require_named`): after a read, discarding its answer when the name
    moved (:meth:`read_natively`), and right before a write (:meth:`write_natively`).
    """

    def __init__(self, ns: LanceNamespace, claims: ClaimStore, resolved: ResolvedTable, *, announces: Callable[[str], bool]) -> None:
        self._ns = ns
        self._claims = claims
        self._resolved = resolved
        self._announces = announces

    @property
    def segments(self) -> list[str]:
        """The name the door is addressed by."""
        return list(self._resolved.segments)

    @property
    def namespace(self) -> LanceNamespace:
        """The connection the name resolves through: for a native describe the door judges itself, and a readback after its commit."""
        return self._ns

    @property
    def resolved(self) -> ResolvedTable:
        """The resolution the router authorized."""
        return self._resolved

    @property
    def location(self) -> str:
        """The location the name resolved to.

        Raises:
            TableNotFoundError: The name named no table.
            NamespaceNotFoundError: The name named no table because its namespace does not exist.
        """
        if self._resolved.location is None:
            missing = NamespaceNotFoundError if self._resolved.namespace_absent else TableNotFoundError
            raise missing(f"table not found: {fga.canonical_object_id(self.segments)}")
        return self._resolved.location

    def require_held(self) -> None:
        """Refuse the door's act unless the location's claim is the one the resolution read and its table is not in the trash.

        Raises:
            TableNotFoundError: The name named no table.
            TableChangedError: The claim changed since the resolution read it, or its table was dropped recoverably.
        """
        location = self.location
        now = location_claims.read(self._claims, location)
        if _etag(now) != _etag(self._resolved.claim):
            self._refuse(location)
        if now is not None and kept_in_trash(self._claims, now.claim):
            self.require_named()

    def require_named(self) -> None:
        """Refuse the door's act unless the name still resolves to the location, under the claim the resolution read.

        Raises:
            TableNotFoundError: The name named no table.
            TableChangedError: The name resolves elsewhere or to nothing now, or the location's claim changed.
        """
        self.require_described_at(resolve(self._ns, self._claims, self.segments).location)

    def require_described_at(self, location: str | None) -> None:
        """:meth:`require_named` for a door whose own native describe, made as it acted, found the name at ``location``.

        Raises:
            TableNotFoundError: The name named no table.
            TableChangedError: ``location`` is not the door's, or the location's claim changed.
        """
        if location != self.location or _etag(location_claims.read(self._claims, location)) != _etag(self._resolved.claim):
            self._refuse(location)

    def _refuse(self, now: str | None) -> None:
        log.warning("door_table_changed", extra={"table": self.segments, "location": self.location, "resolves_to": now})
        raise TableChangedError(f"table {fga.canonical_object_id(self.segments)} changed while this request was being served; describe it and retry")

    def read_natively(self, method: str, *args: object, via: LanceNamespace | None = None) -> Any:  # noqa: ANN401 — the backend's own answer, as `native.call` types it
        """``native.call`` of a read that names the table, through ``via`` (the door's own connection when ``None``), kept only
        while the name still resolves to the door's location (:meth:`require_named`).

        The read may have reached whatever table the name names by then, so its answer, or its failure, is the door's
        only once the name is found still resolving there after it.

        Raises:
            TableChangedError: The table changed since the request's resolution.
        """
        return _checked(lambda: native.call(via if via is not None else self._ns, method, *args), self.require_named)

    def write_natively(self, method: str, *args: object, via: LanceNamespace | None = None) -> Any:  # noqa: ANN401 — the backend's own answer, as `native.call` types it
        """``native.call`` of a write that names the table, through ``via`` (the door's own connection when ``None``), made
        once the name is found still resolving to the door's location (:meth:`require_named`).

        For a write Lance serves only through the namespace: the version finalize (the backend's put-if-not-exists), an
        index drop, whose system-index refusal is the namespace's own, and a backfill. The write takes no location, so a
        drop and a create landing between this check and the backend's own resolution of the name, one describe later,
        would reach the table created; a write Lance serves on a handle is made through :meth:`open` instead.

        Raises:
            TableChangedError: The table changed since the request's resolution.
        """
        self.require_named()
        return native.call(via if via is not None else self._ns, method, *args)

    def finalize_version(self, request: CreateTableVersionRequest, *, turn: str | None = None) -> Any:  # noqa: ANN401 — the backend's own answer, as `native.call` types it
        """Finalize a catalog door's version through the backend's put-if-not-exists (:meth:`write_natively`), once no commit
        but the one holding ``turn`` holds the commit turn of the ref it names ([[LH-259]],
        :mod:`lakehouse_kit.commit_turns`).

        Raises:
            ConcurrentModificationError: Another writer took the version first.
            TableChangedError: The table changed since the request's resolution.
            TurnWaitRefusedError: Every seat a thread waits in was taken.
            UnidentifiedObjectError: The table's location carries no claim recording a uuid.
        """
        self.wait_for_turn(recorded_branch(request.branch), mine=turn)
        return self.write_natively("create_table_version", request)

    def finalize_staged_version(self, request: CreateTableVersionRequest, *, writer: commit_turns.Writer | None, kept: bool) -> Any:  # noqa: ANN401 — the backend's own answer, as `native.call` types it
        """Finalize a stock writer's staged version through the backend's put-if-not-exists (:meth:`write_natively`), once
        its door waited out a door's commit turn in the event loop (:meth:`wait_for_turn_in_loop`), which answered whether
        the record ``kept`` the turn ``writer`` was handed when it lost a race before ([[LH-259]]).

        A writer that loses the race is handed the ref's next turn (:meth:`hand_turn`), so its retry, staged again under the
        same transaction, finalizes before the catalog's own writers of the ref. A retry that reaches its finalize releases
        it; one refused before its finalize, or a writer that never retries, leaves it to lapse at its hold. ``None``, a
        ``Merge``, which pylance never stages again, holds no turn and is handed none.

        Raises:
            ConcurrentModificationError: Another writer took the version first.
            TableChangedError: The table changed since the request's resolution.
            UnidentifiedObjectError: The table's location carries no claim recording a uuid.
        """
        ref = recorded_branch(request.branch)
        token = writer.token if writer is not None else None
        handed = False
        try:
            return self.write_natively("create_table_version", request)
        except TableChangedError:
            raise
        except ConcurrentModificationError:
            handed = self.hand_turn(ref, writer)
            raise
        finally:
            if kept and not handed and token is not None:
                self._turns().release(token)

    def hand_turn(self, ref: str | None, writer: commit_turns.Writer | None) -> bool:
        """Hand ``ref``'s next turn to the stock writer whose commit just lost its race, unless another commit holds the turn
        now or ``writer`` let a handed turn lapse unused within the cooldown; whether it was handed. Never raises: the writer's
        409 is the answer, and a turn not handed leaves its retry an ordinary race.

        A commit whose staged version another writer took is the loser, whether its finalize lost or ``version/create``
        found the version taken before judging it; a version/create naming a version beyond the next is refused before.
        """
        if writer is None:
            return False
        try:
            self._turns().hand(ref, writer)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.warning("commit_turn_not_handed", extra={"table": self.segments, "ref": ref, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return False
        return True

    def wait_for_turn(self, ref: str | None, *, mine: str | None = None) -> None:
        """Return once no commit but the one holding ``mine`` holds ``ref``'s commit turn: a write Lance commits itself asks
        this right before it, and every finalize of a door's commit does (:meth:`finalize_version`). The calling thread
        sleeps while it waits, in one of a bounded number of seats.

        Raises:
            TurnWaitRefusedError: Every seat a thread waits in was taken ([[LH-259]]).
            UnidentifiedObjectError: The table's location carries no claim recording a uuid.
        """
        try:
            self._turns().wait(ref, mine=mine)
        except commit_turns.CommitTurnBusyError as exc:
            raise TurnWaitRefusedError(f"commit refused: {exc}; retry it") from exc

    async def wait_for_turn_in_loop(self, ref: str | None, *, mine: str | None = None, handed: bool = True) -> bool:
        """:meth:`wait_for_turn` from the event loop, holding no request thread while it waits, so a door awaits it before
        it enters the thread pool ([[LH-259]]; measured 2026-10-31: 50 writers waiting in threads made a describe of another
        table wait 26.8 s); then whether the record keeps a turn under ``mine``, read from the store
        (:meth:`commit_turns.TableTurns.keeps`). ``handed=False`` waits out only a catalog door's turn, never one handed to a
        stock writer, which a stock writer's peers race.

        Raises:
            UnidentifiedObjectError: The table's location carries no claim recording a uuid.
        """
        return await self._turns().wait_in_loop(ref, mine=mine, handed=handed)

    @contextmanager
    def commit_turn(self, ref: str | None) -> Generator[str]:
        """``ref``'s commit turn held while the block runs, its token handed to the block: every other finalize of the ref
        waits until the block ends, or the turn lapses (:data:`lakehouse_kit.commit_turns.HOLD`).

        Raises:
            CommitTurnBusyError: Every round of the take lost to another writer of the turn's record; the block did not run.
            UnidentifiedObjectError: The table's location carries no claim recording a uuid.
        """
        with self._turns().held(ref) as token:
            yield token

    def _turns(self) -> commit_turns.TableTurns:
        return commit_turns.TableTurns(
            control_root=self._claims.control_root, storage_options=self._claims.storage_options, table_uuid=self._resolved.require_uuid()
        )

    def open(self, so: StorageOptions, *, version: int | None = None, branch: str | None = None) -> lance.LanceDataset:
        """The checked open of the ref named at the location (``namespace.open_resolved_dataset``), once it still holds the table (:meth:`require_held`).

        Raises:
            TableNotFoundError: The name named no table.
            TableChangedError: The table changed since the request's resolution.
            InvalidTableStateError: The opened ref declares a base the catalog did not sanction.
        """
        return self._opened(open_resolved_dataset, so, version=version, branch=branch)

    def open_unchecked(self, so: StorageOptions, *, version: int | None = None, branch: str | None = None) -> lance.LanceDataset:
        """:meth:`open` without the base judge, for a door that reads no row through the handle.

        Raises:
            TableNotFoundError: The name named no table.
            TableChangedError: The table changed since the request's resolution.
        """
        return self._opened(open_resolved_dataset_unchecked, so, version=version, branch=branch)

    def _opened(self, opener: _Open, so: StorageOptions, *, version: int | None, branch: str | None) -> lance.LanceDataset:
        # Asked after the open: a claim unchanged since the resolution means no other table took the location in
        # between, so the handle holds the manifest of the table the router authorized. Asked when the open fails too,
        # since a table destroyed since its resolution fails the open first, and the change is what the caller needs.
        return _checked(lambda: opener(self.location, so, self.segments, version=version, branch=branch), self.require_held)

    @contextmanager
    def history_held(self, work: history_fences.HistoryWork) -> Generator[None]:
        """The table's history held for ``work`` while the block runs: no reclaim deletes a version while a tag or branch
        is written, and no ref is written while a reclaim runs ([[LH-308]], :mod:`lakehouse_kit.history_fences`).

        Raises:
            ConcurrentModificationError: Work that excludes ``work`` holds the history (another kind, or another branch
                create); the block did not run.
            UnidentifiedObjectError: The table's location carries no claim recording a uuid.
        """
        history = history_fences.TableHistory(
            control_root=self._claims.control_root, storage_options=self._claims.storage_options, table_uuid=self._resolved.require_uuid()
        )
        try:
            with history.held(work):
                yield
        except history_fences.HistoryFencedError as exc:
            log.info("table_history_fenced", extra={"table": self.segments, "work": work, "reason": str(exc)})
            raise ConcurrentModificationError(f"table {fga.canonical_object_id(self.segments)}: {exc}") from exc

    def record_committed_files(self, committed: file_identities.CommittedFiles) -> None:
        """Record what a commit about to land adds to the table, on the control root its claim is kept on, before the
        version is final: maintenance's sweep reads each file again from then on ([[LH-419]]).

        Raises:
            UnidentifiedObjectError: The table's location carries no claim recording a uuid.
        """
        file_identities.record(self._claims.control_root, self._claims.storage_options, self._resolved.require_uuid(), committed)

    def announces(self, operation: str) -> bool:
        """Whether the emitter announces a write of ``operation`` on this table: every one but a row write in the provenance
        namespace (`catalog.services.provenance_namespace`), which the feed holds no row for by design and so keeps no
        author record (ADR 0187)."""
        return self._announces(operation)

    def record_author(self, transaction_uuid: str, *, version: int, ref: str, author: str, manifest: str | None) -> bool:
        """Record ``author``, the caller this door authenticated, as the maker of the commit of ``transaction_uuid`` at
        ``version`` of the ref ``ref`` (:func:`commit_records.ref_key`), its manifest ``manifest`` (``None`` when the record
        is read off the landed version): the one author the reconciler signs a re-announcement of it for (ADR 0187).
        Whether this call created the record, rather than finding it a retry's.

        Raises:
            UnidentifiedObjectError: The table's location carries no claim recording a uuid.
            commit_records.AuthorRecordedError: The transaction is recorded at the version for another commit.
            ServiceUnavailableError: The store refused, or could not be reached for, the record.
        """
        try:
            return commit_records.record_author(
                self._claims.control_root,
                self._claims.storage_options,
                self._resolved.require_uuid(),
                transaction_uuid=transaction_uuid,
                version=version,
                ref=ref,
                author=author,
                manifest=manifest,
            )
        except OSError as exc:
            raise ServiceUnavailableError(f"the object store did not answer recording the author of the commit at version {version}") from exc

    def forget_author(self, transaction_uuid: str, *, version: int) -> None:
        """Delete the author recorded for the commit of ``transaction_uuid`` at ``version``, one that did not land.

        Raises:
            OSError: The store refused the delete.
        """
        commit_records.forget_author(
            self._claims.control_root, self._claims.storage_options, self._resolved.require_uuid(), transaction_uuid=transaction_uuid, version=version
        )


class TableChangedError(ConcurrentModificationError):
    """The table a door resolved changed while the request was served (the spec's code 14).

    Apart from a commit's lost race, which its writer retries by planning again on the new version: a table that
    changed is retried by its caller, who describes it first.
    """


class TurnWaitRefusedError(ConcurrentModificationError):
    """A commit refused before it waited for its ref's commit turn, every seat a thread waits in being taken (the spec's
    code 14, [[LH-259]]). Apart from a lost race, its commit does not plan again: its caller retries, as a bulkhead's
    refused caller does, so a refused writer frees its request thread at once."""


def _checked[T](act: Callable[[], T], check: Callable[[], None]) -> T:
    """``act()``, its answer or its failure the door's only once ``check`` passes after it."""
    try:
        answer = act()
    except Exception:
        check()
        raise
    check()
    return answer


def _etag(claim: ClaimRead | None) -> str | None:
    return claim.etag if claim is not None else None


def door_table(ns: LanceNamespace, store: ClaimStore, segments: list[str], *, announces: Callable[[str], bool]) -> DoorTable:
    """The table ``segments`` names now, for a reader that resolves it outside a request's door (a cron acting on a record),
    whose writes the emitter judges by ``announces``."""
    return DoorTable(ns, store, resolve(ns, store, segments), announces=announces)


def uuid_at(store: ClaimStore, location: str, segments: list[str]) -> str:
    """The uuid the claim on ``location`` records, for the table ``segments`` resolved to there.

    Raises:
        UnidentifiedObjectError: The location carries no claim recording a uuid.
    """
    return _recorded_uuid(location_claims.read(store, location), segments, location)


def _recorded_uuid(claim: ClaimRead | None, segments: list[str], location: str) -> str:
    if claim is None or claim.claim.uuid is None:
        log.error("table_uuid_unrecorded", extra={"table": segments, "location": location})
        raise UnidentifiedObjectError("table", segments, location=location)
    return claim.claim.uuid


def current_name(store: ClaimStore, location: str, uuid: str) -> str | None:
    """The name the table ``uuid`` names holds now: the holder of its location's claim, while that claim records the
    uuid and is not released. ``None`` when the table is gone, or another table holds the location now.

    A record kept for a table names the location and the uuid; a rename hands the claim to the table's new name and
    keeps both, so a surface listing the record names the table as its callers address it now.
    """
    found = location_claims.read(store, location)
    if found is None or found.claim.uuid != uuid or found.claim.released_at is not None:
        return None
    return found.claim.table


class TableRecord(BaseModel):
    """What a record kept for a table records of it: the location and the uuid."""

    location: str
    uuid: str


def names_now(store: ClaimStore, records: list[TableRecord]) -> list[str | None]:
    """The name each record's table holds now (:func:`current_name`), ``None`` for a table that is gone.

    A gone table's record names no table: authorised under the name it kept, it would be shown to whoever holds a
    table created at that name since, which inherits nothing of the old one (ADR 0173 §8).
    """
    return [current_name(store, record.location, record.uuid) for record in records]


def fence(ns: LanceNamespace, store: ClaimStore, table: ResolvedTable, *, holder: str, verb: str) -> DoorFence:
    """Fence the claim ``table`` resolved to, before a door that removes ``holder`` from ``__manifest`` acts on it.

    A claim naming another table that still resolves to the location refuses too, deregister and recoverable drop
    included: it is what the resolution reads when a rename landed between its describe and its claim read, and acting
    on the name then would reach whatever table holds it now. Telling an alias (a second id registered at a held
    location, which no door has formed since [[LH-204]]) from that race would mean resolving the name again.

    Raises:
        TableNotFoundError: The name named no table.
        ConcurrentModificationError: The claim changed since the resolution read it, or another request is acting on
            the table: the name may now name another table.
        InvalidTableStateError: Another table holds the location and still resolves to it.
    """
    if table.location is None:
        raise TableNotFoundError(f"table not found: {holder}")
    token = uuid4().hex
    try:
        location_claims.fence(
            store, table.location, table.claim, table=holder, segments=table.segments, token=token, is_gone=lambda claim: holder_is_gone(ns, store, claim)
        )
    except location_claims.ClaimChangedError:
        raise ConcurrentModificationError(f"cannot {verb} {holder}: it changed while this request was being served; describe it and retry") from None
    except location_claims.LocationHeldError as held:
        log.warning("door_refused_held_location", extra={"table": holder, "location": held.claim.location, "holder": held.claim.table, "verb": verb})
        raise InvalidTableStateError(f"cannot {verb} {holder}: its location is held by another table, which still resolves to it") from None
    return DoorFence(location=table.location, token=token)


def lift_fence(store: ClaimStore, fenced: DoorFence) -> None:
    """Lift a door's fence once it has acted, or failed to. Logged, never raised: the fence lapses on its own after its lease."""
    try:
        location_claims.lift_fence(store, fenced.location, fenced.token)
    except Exception as exc:  # noqa: BLE001 — the door's own answer stands; a fence left behind lapses after LEASE
        log.error("location_claim_fence_not_lifted", extra={"location": fenced.location, "error": str(exc)[:300]})


def hand_over(ns: LanceNamespace, store: ClaimStore, source: ResolvedTable, *, table: str, segments: list[str], previous: str, token: str) -> LocationClaim:
    """Hand the claim ``source`` resolved to from ``previous`` to ``table``, judging a stale holder against this namespace.

    Raises:
        TableNotFoundError: The name named no table.
        LocationHeldError: Another table holds the location, or another request is acting on its holder.
        ClaimChangedError: The claim changed since the resolution read it.
    """
    if source.location is None:
        raise TableNotFoundError(f"table not found: {previous}")
    return location_claims.hand_over(
        store,
        source.location,
        source.claim,
        table=table,
        segments=segments,
        previous=previous,
        token=token,
        is_gone=lambda claim: holder_is_gone(ns, store, claim),
    )


def holder_is_gone(ns: LanceNamespace, store: ClaimStore, claim: LocationClaim) -> bool:
    """Whether ``claim``'s holder no longer resolves to the claimed location.

    A holder in the trash at that location is NOT gone: a recoverable drop keeps its location for the
    undrop, and the purge releases it. Any read that fails answers "not gone", so an outage never frees a
    location.
    """
    try:
        if kept_in_trash(store, claim):
            return False
        try:
            described = native.call(ns, "describe_table", DescribeTableRequest(id=claim.segments))
        except TableNotFoundError:
            return True
        return decoded_path(str(described.location or "")) != claim.location
    except Exception as exc:  # noqa: BLE001 — an unreadable holder is a live one
        log.warning("location_claim_holder_unreadable", extra={"table": claim.table, "location": claim.location, "error": str(exc)[:300]})
        return False


def data_directory_holder_is_gone(ns: LanceNamespace, store: ClaimStore, claim: LocationClaim) -> bool:
    """Whether a data directory's holder no longer exists ([[LH-252]]).

    Unlike a table location, a data directory is never the holder's described location, so the holder
    is judged by existence alone: a table the namespace still describes, or one the trash holds under its name awaiting
    undrop (a data directory's claim records no uuid), keeps its directory. Any read that fails answers "not gone", so
    an outage never frees one.
    """
    try:
        if trash.named(store.control_root, store.storage_options, claim.table):
            return False
        try:
            native.call(ns, "describe_table", DescribeTableRequest(id=claim.segments))
        except TableNotFoundError:
            return True
        return False
    except Exception as exc:  # noqa: BLE001 — an unreadable holder is a live one
        log.warning("data_directory_holder_unreadable", extra={"table": claim.table, "location": claim.location, "error": str(exc)[:300]})
        return False


def kept_in_trash(store: ClaimStore, claim: LocationClaim) -> bool:
    """Whether ``claim`` is kept for the trashed table its uuid names, which a recoverable drop left at this location."""
    record = trash.get(store.control_root, store.storage_options, claim.uuid) if claim.uuid is not None else None
    return record is not None and decoded_path(str(record.get("location") or "")) == claim.location


def take(
    ns: LanceNamespace, store: ClaimStore, location: str, table: str, segments: list[str], *, previous: str | None = None, token: str | None = None
) -> LocationClaim:
    """:func:`lakehouse_kit.location_claims.take` for a table that already exists, judging a stale holder against this namespace.

    The table keeps the uuid its location's claim carries: a rename hands it over, and a recoverable drop and the
    undrops re-take what they hold.

    Raises:
        LocationHeldError: Another table holds the location.
    """
    return location_claims.take(store, location, table, segments, previous=previous, token=token, is_gone=lambda claim: holder_is_gone(ns, store, claim))


def claim_new_table(ns: LanceNamespace, store: ClaimStore, location: str, *, table: str, segments: list[str], table_uuid: str) -> LocationClaim:
    """Make a table this request brings into being the holder of ``location``, under the uuid its door minted (ADR 0173 §2).

    The door mints ``table_uuid`` before the claim: a declaration because the location it declares is named after it
    ([[LH-520]]), a registration because the table's commit marks are kept under it before the claim makes the table one
    the commit reconciler reads ([[LH-550]]). A claim the location
    still holds under ``table``'s name is an ended table's, since the door brings a table of that name into being:
    it is replaced, as a released claim and a gone holder's are, unless the trash keeps it for that table's undrop: its
    name is free again, its location is not ([[LH-525]]). A table attached where an earlier one was
    destroyed, deregistered or is gone is given a new uuid, and nothing granted to the earlier table reaches it.

    Raises:
        LocationHeldError: Another table holds the location.
    """
    return location_claims.take(
        store,
        location,
        table,
        segments,
        uuid=table_uuid,
        is_gone=lambda claim: holder_is_gone(ns, store, claim),
        is_kept=lambda claim: kept_in_trash(store, claim),
    )


def keep_for_trash(ns: LanceNamespace, store: ClaimStore, location: str, table: str, segments: list[str]) -> None:
    """Hold ``location`` for a table a recoverable drop is about to detach, so no other id registers over its bytes.

    A location another table already holds is left with that holder and logged: the drop is still
    correct, the purge refuses to delete bytes a live id resolves to, and the undrop refuses to alias it.
    """
    try:
        take(ns, store, location, table, segments)
    except location_claims.LocationHeldError as held:
        log.warning("trash_location_held_by_another_table", extra={"table": table, "holder": held.claim.table, "location": held.claim.location})


def require_no_other_holder(ns: LanceNamespace, store: ClaimStore, location: str, table: str) -> None:
    """Refuse destroying the bytes at ``location`` while its claim names ANOTHER table that still resolves to them.

    A destructive drop deletes the location, not the id: when another id holds it — an alias the doors
    cannot form any more, or one already on the estate — the drop would destroy that table's bytes while it
    goes on resolving. No claim, or a claim held by ``table`` or by a holder that is gone, lets the drop
    proceed; the manifest alone cannot say which of two ids owns the bytes, the claim can.

    Raises:
        InvalidTableStateError: Another live table holds the location.
    """
    current = location_claims.holder(store, location)
    if current is None or current.table == table or holder_is_gone(ns, store, current):
        return
    log.warning("drop_refused_held_location", extra={"table": table, "location": current.location, "holder": current.table})
    raise InvalidTableStateError(f"cannot destroy table {table}: its location is held by another table, which still resolves to it")
