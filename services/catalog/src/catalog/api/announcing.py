"""A door's announcements, filed before its change and sent from that filing ([[LH-576]]).

A door builds what it will announce before it changes anything, wraps its change and its sends in
:meth:`Announcer.announcing`, and sends each message through the :class:`Filing` that yields, the path the finish takes
too, so what a finish sends for a failed door is exactly what that door would have sent
(:mod:`catalog.services.announcement_intents` says why).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, InvalidInputError, ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict

from catalog.api.authz import decisions
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, ObjectUuidsDep, Resolver, ResolverDep, SettingsDep, app_wire, originator_hint
from catalog.core.config import Settings
from catalog.core.lineage_emit import LineageEmitter, UuidReader, emit_write_event
from catalog.schemas import AnnouncementIntentsFinishResponse, RoleRecord
from catalog.services import announcement_intents, roles, subject_tuples, table_claims
from catalog.services.announcement_intents import (
    Announcement,
    ControlMessage,
    Evidence,
    LineageMarker,
    Message,
    NameResolves,
    NamespaceResolves,
    RecordGone,
    RecordHeld,
    RoleEnded,
    Standing,
    TrashedUnder,
    TupleGone,
    TupleHeld,
    Unregistered,
)
from lakehouse.events.control_emit import ControlEmitter
from lakehouse.events.control_events import CatalogControlEvent
from lakehouse.events.lineage_events import DatasetUuids
from lakehouse.governed import fga_ids, fga_reads, fga_writes
from lakehouse.problems.outage import store_refusal
from lakehouse.records import location_claims, trash
from lakehouse.storage import conditional_records
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

_meter = metrics.get_meter("lance.catalog")
_left_standing = _meter.create_counter(
    "catalog.announcement_intents.left",
    unit="{filing}",
    description="Filings a finish left standing, by why: `young` (its door may still be at work), `at_work` (a door is "
    "acting on the table a drop, a deregister or a cascade filed for) or `failed` (judging it raised, logged as "
    "`announcement_intent_unjudged` with its key).",
)
_unreadable = _meter.create_counter(
    "catalog.announcement_intents.unreadable",
    unit="{filing}",
    description="Filings a finish removed unread: no filing with evidence this catalog reads exactly, so never judgeable "
    "and nothing of them sent; each is logged as `announcement_intent_unreadable` with its key and the store's stamp.",
)
_oldest_standing = _meter.create_gauge(
    "catalog.announcement_intents.oldest",
    unit="s",
    description="How long the oldest filing the last finish pass left standing has stood, on the store's clock, as this "
    "replica found it: 0 when that pass left none. A filing stands at most its lease and one audit while every judgement of "
    "it settles, and while it stands its object's trash is neither released nor purged.",
)


#: The oldest age, in seconds, of a filing the pass under way has left standing; a pass begins with a call naming no cursor.
_oldest_left = [0.0]


def _left(age: timedelta, reason: str) -> None:
    """Count a filing of ``age`` left standing for ``reason``, and keep its age where it is the pass's oldest so far."""
    _left_standing.add(1, {"lance.catalog.reason": reason})
    _oldest_left[0] = max(_oldest_left[0], age.total_seconds())


def _second(at: datetime) -> datetime:
    return at.replace(microsecond=0)


def _create_left_series() -> None:
    """Create every reason's series at 0, so the first filing left for one is an increase."""
    for reason in ("young", "at_work", "failed"):
        _left_standing.add(0, {"lance.catalog.reason": reason})
    _unreadable.add(0)


type GrantAction = Literal["grant_added", "grant_revoked"]
#: A tuple's place in what a door filed: its object, relation and user.
type TupleKey = tuple[str, str, str]


class DropAtWorkError(Exception):
    """A door is acting on the table a drop, a deregister or a cascade filed for, or a recoverable drop's own fence still
    stands, so whether that door retired it is not yet the store's to tell: its filing stands for a later judgement."""


class WithdrawalLeftError(DropAtWorkError):
    """A recoverable drop did not detach its table, and its trash record is left for the finish to withdraw: the door
    judging it may not fence the claim it has read."""


class Announcer:
    """Where one request's announcements are filed, judged and sent: the store the filings live on, OpenFGA and the
    request's resolver to judge a change by, and the two lanes (:func:`get_announcer`)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: OpenFgaClient | None,
        resolver: Resolver,
        control: ControlEmitter,
        lineage: LineageEmitter,
        uuids: UuidReader,
        originator: str | None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._resolver = resolver
        self._control = control
        self._lineage = lineage
        self._uuids = uuids
        self._originator = originator

    def marker(
        self,
        segments: list[str],
        *,
        author: str | None,
        operation: str,
        uuids: DatasetUuids,
        source_uri: str | None = None,
        renamed_from: list[str] | None = None,
    ) -> LineageMarker:
        """A table's lifecycle marker as this request announces it, its time fixed now."""
        return LineageMarker(
            segments=segments,
            author=author,
            operation=operation,
            uuids=uuids,
            source_uri=source_uri,
            renamed_from=renamed_from,
            originator=self._originator,
            event_time=datetime.now(UTC).isoformat(),
        )

    @asynccontextmanager
    async def announcing(self, *, door: str, target: str, announcements: Sequence[Announcement]) -> AsyncGenerator[Filing]:
        """File ``announcements`` before the change ``door`` makes on ``target``, and remove the filing once the block
        returns; a block that raises leaves it, which the finish sends from once the store shows each change landed.

        What an earlier request filed on ``target`` is read first, and each announcement of the same change for the same
        caller (:func:`announcement_intents.identity`) is reused, so this door, that request and that filing's finish send
        one event under one id, whichever of them landed the change. A filing older than
        :data:`announcement_intents.LEASE`, its door gone, is finished here once this door's own filing carries what it
        reuses. A list with nothing in it files nothing.

        Raises:
            ServiceUnavailableError: The store or OpenFGA did not answer a read, or the store the filing; nothing was changed.
            InternalError: The store refused the filing; nothing was changed.
            ConcurrentModificationError: A recoverable drop filed on ``target`` is not settled yet; nothing was changed.
        """
        if not announcements:
            yield Filing(self._send, {})
            return
        root, so = self._settings.registry_root, self._settings.storage_options()
        try:
            earlier = (await run_in_threadpool(partial(announcement_intents.standing, root, so, target=target))).filings
        except OSError as exc:
            raise store_refusal(exc, reading=f"what was filed on {target}", so="nothing was changed") from exc
        reused, left = await self._take_over(earlier, wanted={announcement_intents.identity(a) for a in announcements})
        filed = [_as_filed(a, reused) for a in announcements]
        try:
            key = await run_in_threadpool(announcement_intents.file, root, so, door=door, target=target, announcements=filed)
        except OSError as exc:
            raise store_refusal(exc, reading=f"filing what {door} will announce", so="nothing was changed") from exc
        for filing, landed in left:
            for announcement in landed:
                await self._send(announcement.message)
            await self._settle(filing.key)
        yield Filing(self._send, reused)
        # The change and its announcements went out; a filing left standing is sent again by the finish, as the same events.
        await self._settle(key)

    async def _take_over(self, earlier: list[Standing], *, wanted: set[str]) -> tuple[dict[str, Announcement], list[tuple[Standing, list[Announcement]]]]:
        """The oldest earlier filing's announcement of each identity in ``wanted``, which this door's of that identity is one
        event with (``announcement_intents.reusing``), and each filing its door left, with what of it landed. A filing whose
        door may still be at work is not judged: that door sends what it filed."""
        reused: dict[str, Announcement] = {}
        left: list[tuple[Standing, list[Announcement]]] = []
        for filing in earlier:
            for announcement in filing.intent.announcements:
                if (key := announcement_intents.identity(announcement)) in wanted:
                    reused.setdefault(key, announcement)
            if not filing.young:
                try:
                    # Judged without withdrawing a drop's trash record: that fences the claim this door has read.
                    landed = [a for a in filing.intent.announcements if await self.landed(a.evidence, withdrawing=False, filed_at=filing.filed_at)]
                    left.append((filing, landed))
                except WithdrawalLeftError:
                    continue
                except DropAtWorkError as at_work:
                    # A drop on this door's own object whose fence may lapse at any moment: acting now, an undrop could
                    # restore the table its drop detached before that drop is told, and the drop would never be.
                    raise ConcurrentModificationError(f"{at_work}; retry once it has settled") from at_work
        return reused, left

    async def finish(self, *, limit: int, after: str | None = None) -> AnnouncementIntentsFinishResponse:
        """Send what each filing older than :data:`announcement_intents.LEASE` names wherever the store shows its change
        landed, and remove the filing, at most ``limit`` filings, oldest first, from past the cursor ``after`` an earlier
        call of the same pass returned: the answer's ``next`` resumes past every filing this call looked at, so a pass
        reaches the filings behind those it left standing.

        A filing whose judgement raises anything but an outage is left standing and logged, and the rest are finished.

        Raises:
            ServiceUnavailableError: The store or OpenFGA did not answer a read; the filing being judged stands for the next.
            InvalidInputError: ``after`` is no cursor a finish returned.
        """
        root, so = self._settings.registry_root, self._settings.storage_options()
        try:
            read = await run_in_threadpool(partial(announcement_intents.standing, root, so, limit=limit, after=after))
        except OSError as exc:
            raise store_refusal(exc, reading="the filed announcements", so="nothing was sent") from exc
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc
        done = AnnouncementIntentsFinishResponse(left=max(read.listed - limit, 0), next=read.next)
        _create_left_series()
        if after is None:  # a pass begins: what it reports is the oldest filing it leaves standing
            _oldest_left[0] = 0.0
        for key, filed_at, age in read.unread:
            # Within its lease an unread filing waits: a door still running under another version of the code (a rolling
            # upgrade) may have filed it, and may yet send it and settle it itself.
            if age < announcement_intents.LEASE:
                done.young += 1
                continue
            # REMOVED UNREAD. A filing this code cannot read exactly (`announcement_intents._Evidence`) can never be
            # judged by it, so it announces nothing; left, it would stand for good and keep the standing-filing alert
            # firing. The ERROR line and the counter are its trace, without its body, which can name principals. A
            # filing is written once, create-if-absent under a key of its own, so nothing rewrites it between the read
            # and the delete.
            log.error("announcement_intent_unreadable", extra={"key": key, "filed_at": filed_at.isoformat()})
            try:
                await run_in_threadpool(announcement_intents.settle, root, so, key)
            except OSError as exc:
                raise store_refusal(exc, reading=f"removing the unreadable filing {key}", so="nothing of it was sent") from exc
            _unreadable.add(1)
        for filing in read.filings:
            if filing.young:
                done.young += 1
                _left(filing.age, "young")
                continue
            try:
                landed = [a for a in filing.intent.announcements if await self.landed(a.evidence, filed_at=filing.filed_at)]
            except DropAtWorkError:
                done.young += 1
                _left(filing.age, "at_work")
                continue
            except ServiceUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 — one filing that cannot be judged must not stop every filing after it
                # Judged alone: a record or a claim that cannot be read leaves this filing standing, said once a pass by
                # its key, and the rest of the batch is finished.
                log.error(
                    "announcement_intent_unjudged",
                    extra={"key": filing.key, "door": filing.intent.door, "target": filing.intent.target, "error": type(exc).__name__},
                )
                done.failed += 1
                _left(filing.age, "failed")
                continue
            done.unlanded += len(filing.intent.announcements) - len(landed)
            for announcement in landed:
                await self._send(announcement.message)
                done.sent += 1
            try:
                await run_in_threadpool(announcement_intents.settle, root, so, filing.key)
            except OSError as exc:
                raise store_refusal(exc, reading=f"removing the finished filing {filing.key}", so="its announcements went out") from exc
            done.finished += 1
        _oldest_standing.set(_oldest_left[0])
        return done

    async def landed(self, evidence: Evidence, *, withdrawing: bool = True, filed_at: datetime | None = None) -> bool:
        """Whether the store shows the change ``evidence`` names landed. A recoverable drop judged not to have detached its
        table has its trash record withdrawn (:meth:`_drop_landed`), unless ``withdrawing`` is off. ``filed_at`` is the
        store's stamp on the filing the evidence came from, which a trash record written since is told by.

        Raises:
            ServiceUnavailableError: The store or OpenFGA did not answer.
            trash.UnreadableTrashRecordError: A trash record is filed and is none, so it shows neither outcome.
            DropAtWorkError: A drop's, a deregister's or a cascade's outcome cannot be told yet.
        """
        match evidence:
            case TupleGone() | TupleHeld():
                if self._client is None:  # authorization off: OpenFGA holds no tuple
                    return isinstance(evidence, TupleGone)
                held, _ = await fga_reads.read_tuples(self._client, user=evidence.user, relation=evidence.relation, obj=evidence.object)
                return bool(held) if isinstance(evidence, TupleHeld) else not held
            case NameResolves():
                resolved = await self._resolver.table(evidence.segments)
                return resolved.location is not None and resolved.uuid == evidence.uuid
            case Unregistered():
                return await self._unregistered(evidence, filed_at=filed_at)
            case NamespaceResolves():
                names_it = await self._resolver.namespace_uuid(evidence.segments) == evidence.uuid
                return names_it if evidence.held else not names_it
            case RecordGone(key=key):
                return await self._absent(lambda root, so: conditional_records.read_json(root, so, key))
            case RecordHeld(key=key):
                return not await self._absent(lambda root, so: conditional_records.read_json(root, so, key))
            case RoleEnded(project=project, role=role):
                return await self._absent(lambda root, so: roles.find_role(root, so, project, role_id=role))
            case TrashedUnder(uuid=uuid, attempt=drop):
                return await self._drop_landed(uuid, drop, withdrawing=withdrawing)

    async def _drop_landed(self, uuid: str, drop: str, *, withdrawing: bool) -> bool:
        """Whether the recoverable drop ``drop`` detached the table ``uuid`` names.

        Its trash record proves it once stamped (``trash.stamp_detached``), and no record of this drop's own disproves it.
        A record of its own left unstamped (the drop ended between its record and its stamp) is judged from the claim on
        the table's location: read, the holder it names resolved, read again unchanged. While a fence on it stands, a door
        is at work and nothing is told. Every door that unregisters the table or moves it to another name (a rename, a
        drop, a deregister, a cascade) writes the claim first, fencing it, and can only fence a table still registered.
        An undrop may take the claim without writing it, since the claim already names the table, but an undrop only
        makes the table resolve, which this judgement reads as not landed. So a claim still fenced by this drop, whose
        holder names no such table, shows that nothing but this drop's detach unregistered the table: it is stamped, and
        landed. Any other claim shows the table registered, or moved by a door that found it registered, after this drop
        fenced it: the drop did not detach it, and its record, which would keep a live table out of maintenance and
        answer for a drop that never happened, is withdrawn under a fence of the finish's own.
        """
        store = table_claims.claim_store(self._settings)
        root, so = store.control_root, store.storage_options
        record = await self._read(partial(trash.get, root, so, uuid))
        if record is None or record.drop != drop:
            return False
        if record.detached:
            return True
        first = await self._read(partial(location_claims.read, store, record.location))
        if first is None or first.claim.uuid != uuid or first.claim.released_at is not None:
            return await self._withdrawn(record, None, withdrawing=withdrawing)
        holder = await self._resolver.table_now(first.claim.segments)
        second = await self._read(partial(location_claims.read, store, record.location))
        now = await self._read(partial(location_claims.store_now, store, record.location))
        fence = second.claim.fence if second is not None else None
        if second is None or second.etag != first.etag or (fence is not None and now - fence.at < location_claims.LEASE):
            raise DropAtWorkError(f"a door is at work on table {uuid}")
        if (holder.location is None or holder.uuid != uuid) and fence is not None and fence.token == drop:
            if not await self._read(partial(trash.stamp_detached, root, so, record)):
                raise DropAtWorkError(f"the trash record of table {uuid} changed while it was judged")
            return True
        return await self._withdrawn(record, second, withdrawing=withdrawing)

    async def _unregistered(self, evidence: Unregistered, *, filed_at: datetime | None) -> bool:
        """Whether the table ``evidence`` names is registered under no name, and kept by the trash, by a record written no
        earlier than ``filed_at``, only where its door retires a table into it.

        The record is ordered against the filing by the store's own stamps on both, never by a pod's clock (``dropped_at``
        is the dropping pod's): a record written before the filing is a stale one a live table carried, which neither
        shows nor refutes this door's retire. The stamp is the record's last write, so a stale record rewritten since
        the filing reads as written since; the trash release's wait for the finish notes nothing on it for that reason.
        With no ``filed_at`` every record counts.

        The claim on its location is read, the door's name and the name the claim holds the table under resolved, and
        the claim read again unchanged: a rename hands the claim over, fenced, before its destination is registered, so
        a claim that changed or carries a fence within its lease shows a door at work, and nothing is told yet.

        Raises:
            DropAtWorkError: A door is acting on the table's location.
        """
        store = table_claims.claim_store(self._settings)
        first = await self._read(partial(location_claims.read, store, evidence.location))
        names = [evidence.segments]
        if first is not None and first.claim.uuid == evidence.uuid and first.claim.released_at is None and first.claim.segments != evidence.segments:
            names.append(first.claim.segments)
        for name in names:
            resolved = await self._resolver.table_now(name)
            if resolved.location is not None and resolved.uuid == evidence.uuid:
                return False
        second = await self._read(partial(location_claims.read, store, evidence.location))
        now = await self._read(partial(location_claims.store_now, store, evidence.location))
        if (first.etag if first else None) != (second.etag if second else None) or (second is not None and second.claim.fenced_against(None, now)):
            raise DropAtWorkError(f"a door is at work on table {evidence.uuid}")
        at = trash.key(evidence.uuid)
        stored = (await self._read(partial(conditional_records.read_json_at, store.control_root, store.storage_options, at))).stored
        kept = trash.read(stored.record, at=at) if stored is not None else None
        # Whole seconds on both sides: a GET answers the record's stamp to the second, a listing the filing's finer, so a
        # record written in the filing's own second would otherwise sort before it. A tie counts as written since: that
        # can lose a dead door's event, never send a false one.
        since = stored is not None and kept is not None and not kept.is_residue and (filed_at is None or _second(stored.modified) >= _second(filed_at))
        return since == evidence.trashed

    async def _withdrawn(self, record: trash.TrashRecord, claim: location_claims.ClaimRead | None, *, withdrawing: bool) -> bool:
        """Withdraw ``record``, which a drop that never detached its table filed, under a fence on ``claim`` taken as read;
        ``False``, the drop not landed.

        Raises:
            DropAtWorkError: ``withdrawing`` is off, or the claim or the record changed since it was read.
        """
        if not withdrawing:
            raise WithdrawalLeftError(f"the trash record of table {record.uuid} is left for the finish to withdraw")
        store = table_claims.claim_store(self._settings)
        fenced = table_claims.DoorFence(location=record.location, token=uuid4().hex)
        if claim is not None:
            table, segments = claim.claim.table, claim.claim.segments
            fencing = partial(location_claims.fence, store, record.location, claim, table=table, segments=segments, token=fenced.token, is_gone=lambda _: False)
            try:
                await self._read(fencing)
            except (location_claims.LocationHeldError, location_claims.ClaimChangedError):
                raise DropAtWorkError(f"the claim of table {record.uuid} changed while it was judged") from None
        try:
            withdrawn = await self._read(partial(trash.withdraw, store.control_root, store.storage_options, record, claims=store, token=fenced.token))
        finally:
            if claim is not None:
                await run_in_threadpool(table_claims.lift_fence, store, fenced)
        if not withdrawn:
            raise DropAtWorkError(f"the trash record of table {record.uuid} changed while it was judged")
        log.warning("drop_withdrawn", extra={"uuid": record.uuid, "drop": record.drop, "reason": "the drop never detached its table"})
        return False

    async def _read[T](self, read: Callable[[], T]) -> T:
        try:
            return await run_in_threadpool(read)
        except OSError as exc:
            raise store_refusal(exc, reading="whether a filed change landed", so="nothing was sent") from exc

    async def _absent(self, read: Callable[[str, StorageOptions], object]) -> bool:
        try:
            return await run_in_threadpool(read, self._settings.registry_root, self._settings.storage_options()) is None
        except OSError as exc:
            raise store_refusal(exc, reading="whether a filed change landed", so="nothing was sent") from exc

    async def _send(self, message: Message) -> None:
        """Emit ``message`` on its lane."""
        if isinstance(message, ControlMessage):
            await self._control.emit(message.event)
            return
        await emit_write_event(
            self._lineage,
            message.segments,
            author=message.author,
            version=None,
            operation=message.operation,
            uuids=self._uuids,
            known=message.uuids,
            source_uri=message.source_uri,
            renamed_from=message.renamed_from,
            event_time=message.event_time,
            originator=message.originator,
        )

    async def _settle(self, key: str) -> None:
        try:
            await run_in_threadpool(announcement_intents.settle, self._settings.registry_root, self._settings.storage_options(), key)
        except OSError as exc:
            log.warning("announcement_intent_unsettled", extra={"key": key, "error": str(exc)[:300]})


class Filing:
    """What a door filed, which it sends through: each announcement as one event with what an earlier request filed of
    the same change, where one did."""

    def __init__(self, send: Callable[[Message], Awaitable[None]], reused: Mapping[str, Announcement]) -> None:
        self._send = send
        self._reused = reused

    def of(self, announcement: Announcement) -> Announcement:
        """``announcement`` as this filing sends it: under an earlier request's id where that request filed the same change.
        A door may send its event with what it learned making the change (a count), under the id it filed."""
        return _as_filed(announcement, self._reused)

    async def send(self, announcement: Announcement) -> None:
        """Send ``announcement`` as filed, with this request's bearer."""
        await self._send(self.of(announcement).message)

    async def send_grant_changes(
        self, filed: Mapping[TupleKey, Announcement], changed: Iterable[fga_writes.ClientTuple], *, action: GrantAction, actor: str | None, reason: str
    ) -> None:
        """Announce each tuple of ``changed`` naming someone as the door filed it; a tuple the door's change reached that
        its filing did not name, one written between the door's read and its change, is announced as built now."""
        for t in changed:
            if fga_ids.names_someone(t.user):
                await self.send(filed.get((t.object, t.relation, t.user)) or _grant_change(t, action=action, actor=actor, reason=reason))


def _as_filed(announcement: Announcement, reused: Mapping[str, Announcement]) -> Announcement:
    earlier = reused.get(announcement_intents.identity(announcement))
    return announcement if earlier is None else announcement_intents.reusing(announcement, earlier)


def grant_changes(tuples: Iterable[fga_writes.ClientTuple], *, action: GrantAction, actor: str | None, reason: str) -> dict[TupleKey, Announcement]:
    """What a door files before it adds (``grant_added``) or revokes (``grant_revoked``) ``tuples``: one announcement per
    tuple naming someone a notifier can address (``fga_ids.names_someone``), keyed by the tuple, each landed when OpenFGA holds
    the tuple, or holds it no longer."""
    return {(t.object, t.relation, t.user): _grant_change(t, action=action, actor=actor, reason=reason) for t in tuples if fga_ids.names_someone(t.user)}


class RoleEnding(BaseModel):
    """What a door files before it ends a role: the role's end, and each grant the end takes away."""

    model_config = ConfigDict(frozen=True)

    ended: Announcement
    grants: dict[TupleKey, Announcement]

    def announcements(self) -> list[Announcement]:
        return [self.ended, *self.grants.values()]

    async def send(self, filing: Filing, removed: Iterable[fga_writes.ClientTuple], *, actor: str | None, reason: str) -> None:
        """Announce the role ended and each grant ``removed`` took, as filed."""
        await filing.send(self.ended)
        await filing.send_grant_changes(self.grants, removed, action="grant_revoked", actor=actor, reason=reason)


async def role_ending(client: OpenFgaClient | None, role: RoleRecord, *, actor: str | None, reason: str) -> RoleEnding:
    """What ending ``role`` will announce, read as its end will read it (``role_grants.end_role``): the tuples naming the
    role and those on it. With ``client`` ``None`` (authorization off) its end takes no grant.

    Raises:
        ServiceUnavailableError: OpenFGA did not answer a read.
    """
    held: list[fga_writes.ClientTuple] = []
    if client is not None:
        role_obj = roles.role_object(role.id)
        held = [
            *await subject_tuples.tuples_naming(client, roles.assignees_of(role.id)),
            *(t for t in await fga_reads.read_object_tuples(client, role_obj) if t.object == role_obj),
        ]
    ended = CatalogControlEvent(
        action="role_deleted",
        object_type="role",
        object_id=roles.role_object(role.id),
        actor=actor,
        extra={"project": role.project, "name": role.name, "deleted": len(held)},
    )
    return RoleEnding(
        ended=Announcement(evidence=RoleEnded(project=role.project, role=str(role.id)), message=ControlMessage(event=ended)),
        grants=grant_changes(held, action="grant_revoked", actor=actor, reason=reason),
    )


def grant_announcement(
    t: fga_writes.ClientTuple,
    *,
    action: GrantAction,
    actor: str | None,
    object_name: str | None = None,
    object_uuid: str | None = None,
    namespace_uuid: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> Announcement:
    """The control event a door announces adding (``grant_added``) or revoking (``grant_revoked``) ``t``, landed when OpenFGA
    holds the tuple, or holds it no longer; ``extra`` adds to the relation and subject every grant event names."""
    event = CatalogControlEvent(
        action=action,
        object_type="grant",
        object_id=t.object,
        object_name=object_name,
        object_uuid=object_uuid,
        namespace_uuid=namespace_uuid,
        actor=actor,
        extra={"relation": t.relation, "subject": t.user, **(extra or {})},
    )
    evidence: Evidence = (
        TupleHeld(user=t.user, relation=t.relation, object=t.object)
        if action == "grant_added"
        else TupleGone(user=t.user, relation=t.relation, object=t.object)
    )
    return Announcement(evidence=evidence, message=ControlMessage(event=event))


def _grant_change(t: fga_writes.ClientTuple, *, action: GrantAction, actor: str | None, reason: str) -> Announcement:
    return grant_announcement(t, action=action, actor=actor, extra={"reason": reason})


def get_announcer(
    request: Request, *, settings: SettingsDep, client: FgaClientDep, resolver: ResolverDep, control: ControlEmitterDep, uuids: ObjectUuidsDep
) -> Announcer:
    """This request's :class:`Announcer`. Its lineage emitter is the app's own, unbound to the request: a marker carries the
    originator its filing request claimed, whichever request sends it. The headers are read off the request, so no door
    gains a documented parameter by announcing."""
    return Announcer(
        settings,
        client=decisions.wired(settings, client),
        resolver=resolver,
        control=control,
        lineage=app_wire(request, "lineage_emitter", LineageEmitter),
        uuids=uuids,
        originator=originator_hint(request.headers.get("x-lance-originator")),
    )


AnnouncerDep = Annotated[Announcer, Depends(get_announcer)]
