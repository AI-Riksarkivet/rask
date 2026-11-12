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
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, ObjectUuidsDep, Resolver, ResolverDep, SettingsDep, originator_hint
from catalog.core.config import Settings
from catalog.core.lineage_emit import LineageEmitter, NoopEmitter, UuidReader, emit_write_event
from catalog.schemas import AnnouncementIntentsFinishResponse, RoleRecord
from catalog.services import announcement_intents, roles, subject_tuples
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
    TupleGone,
    TupleHeld,
)
from lakehouse import records
from lakehouse.control_emit import ControlEmitter
from lakehouse.control_events import CatalogControlEvent
from lakehouse.governed import fga
from lakehouse.lineage_events import DatasetUuids
from lakehouse.outage import store_refusal
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

type GrantAction = Literal["grant_added", "grant_revoked"]
#: A tuple's place in what a door filed: its object, relation and user.
type TupleKey = tuple[str, str, str]


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
        authorization: str | None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._resolver = resolver
        self._control = control
        self._lineage = lineage
        self._uuids = uuids
        self._originator = originator
        self._authorization = authorization

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
        """
        if not announcements:
            yield Filing(partial(self._send, authorization=self._authorization), {})
            return
        root, so = self._settings.registry_root, self._settings.storage_options()
        try:
            earlier, _listed = await run_in_threadpool(partial(announcement_intents.standing, root, so, target=target))
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
                await self._send(announcement.message, authorization=None)
            await self._settle(filing.key)
        yield Filing(partial(self._send, authorization=self._authorization), reused)
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
                left.append((filing, [a for a in filing.intent.announcements if await self.landed(a.evidence)]))
        return reused, left

    async def finish(self, *, limit: int) -> AnnouncementIntentsFinishResponse:
        """Send what each filing older than :data:`announcement_intents.LEASE` names wherever the store shows its change
        landed, and remove the filing, at most ``limit`` filings, oldest first.

        Raises:
            ServiceUnavailableError: The store or OpenFGA did not answer a read; the filing being judged stands for the next.
        """
        root, so = self._settings.registry_root, self._settings.storage_options()
        try:
            filings, listed = await run_in_threadpool(partial(announcement_intents.standing, root, so, limit=limit))
        except OSError as exc:
            raise store_refusal(exc, reading="the filed announcements", so="nothing was sent") from exc
        done = AnnouncementIntentsFinishResponse(left=max(listed - limit, 0))
        for filing in filings:
            if filing.young:
                done.young += 1
                continue
            for announcement in filing.intent.announcements:
                if not await self.landed(announcement.evidence):
                    done.unlanded += 1
                    continue
                await self._send(announcement.message, authorization=None)
                done.sent += 1
            try:
                await run_in_threadpool(announcement_intents.settle, root, so, filing.key)
            except OSError as exc:
                raise store_refusal(exc, reading=f"removing the finished filing {filing.key}", so="its announcements went out") from exc
            done.finished += 1
        return done

    async def landed(self, evidence: Evidence) -> bool:
        """Whether the store shows the change ``evidence`` names landed.

        Raises:
            ServiceUnavailableError: The store or OpenFGA did not answer.
        """
        match evidence:
            case TupleGone() | TupleHeld():
                if self._client is None:  # authorization off: OpenFGA holds no tuple
                    return isinstance(evidence, TupleGone)
                held, _ = await fga.read_tuples(self._client, user=evidence.user, relation=evidence.relation, obj=evidence.object)
                return bool(held) if isinstance(evidence, TupleHeld) else not held
            case NameResolves():
                resolved = await self._resolver.table(evidence.segments)
                names_it = resolved.location is not None and resolved.uuid == evidence.uuid
                return names_it if evidence.held else not names_it
            case NamespaceResolves():
                names_it = await self._resolver.namespace_uuid(evidence.segments) == evidence.uuid
                return names_it if evidence.held else not names_it
            case RecordGone(key=key):
                return await self._absent(lambda root, so: records.read_json(root, so, key))
            case RecordHeld(key=key):
                return not await self._absent(lambda root, so: records.read_json(root, so, key))
            case RoleEnded(project=project, role=role):
                return await self._absent(lambda root, so: roles.find_role(root, so, project, role))

    async def _absent(self, read: Callable[[str, StorageOptions], object]) -> bool:
        try:
            return await run_in_threadpool(read, self._settings.registry_root, self._settings.storage_options()) is None
        except OSError as exc:
            raise store_refusal(exc, reading="whether a filed change landed", so="nothing was sent") from exc

    async def _send(self, message: Message, *, authorization: str | None) -> None:
        """Emit ``message`` on its lane. ``authorization`` is the bearer of the caller the message is about, which only the
        http lineage transport reads; one sent for another request's filing goes without."""
        if isinstance(message, ControlMessage):
            await self._control.emit(message.event)
            return
        await emit_write_event(
            self._lineage,
            message.segments,
            author=message.author,
            version=None,
            operation=message.operation,
            authorization=authorization,
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
        self, filed: Mapping[TupleKey, Announcement], changed: Iterable[fga.ClientTuple], *, action: GrantAction, actor: str | None, reason: str
    ) -> None:
        """Announce each tuple of ``changed`` naming someone as the door filed it; a tuple the door's change reached that
        its filing did not name, one written between the door's read and its change, is announced as built now."""
        for t in changed:
            if fga.names_someone(t.user):
                await self.send(filed.get((t.object, t.relation, t.user)) or _grant_change(t, action=action, actor=actor, reason=reason))


def _as_filed(announcement: Announcement, reused: Mapping[str, Announcement]) -> Announcement:
    earlier = reused.get(announcement_intents.identity(announcement))
    return announcement if earlier is None else announcement_intents.reusing(announcement, earlier)


def grant_changes(tuples: Iterable[fga.ClientTuple], *, action: GrantAction, actor: str | None, reason: str) -> dict[TupleKey, Announcement]:
    """What a door files before it adds (``grant_added``) or revokes (``grant_revoked``) ``tuples``: one announcement per
    tuple naming someone a notifier can address (``fga.names_someone``), keyed by the tuple, each landed when OpenFGA holds
    the tuple, or holds it no longer."""
    return {(t.object, t.relation, t.user): _grant_change(t, action=action, actor=actor, reason=reason) for t in tuples if fga.names_someone(t.user)}


class RoleEnding(BaseModel):
    """What a door files before it ends a role: the role's end, and each grant the end takes away."""

    model_config = ConfigDict(frozen=True)

    ended: Announcement
    grants: dict[TupleKey, Announcement]

    def announcements(self) -> list[Announcement]:
        return [self.ended, *self.grants.values()]

    async def send(self, filing: Filing, removed: Iterable[fga.ClientTuple], *, actor: str | None, reason: str) -> None:
        """Announce the role ended and each grant ``removed`` took, as filed."""
        await filing.send(self.ended)
        await filing.send_grant_changes(self.grants, removed, action="grant_revoked", actor=actor, reason=reason)


async def role_ending(client: OpenFgaClient | None, role: RoleRecord, *, actor: str | None, reason: str) -> RoleEnding:
    """What ending ``role`` will announce, read as its end will read it (``role_grants.end_role``): the tuples naming the
    role and those on it. With ``client`` ``None`` (authorization off) its end takes no grant.

    Raises:
        ServiceUnavailableError: OpenFGA did not answer a read.
    """
    held: list[fga.ClientTuple] = []
    if client is not None:
        role_obj = roles.role_object(role.id)
        held = [
            *await subject_tuples.tuples_naming(client, roles.assignees_of(role.id)),
            *(t for t in await fga.read_object_tuples(client, role_obj) if t.object == role_obj),
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
    t: fga.ClientTuple,
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


def _grant_change(t: fga.ClientTuple, *, action: GrantAction, actor: str | None, reason: str) -> Announcement:
    return grant_announcement(t, action=action, actor=actor, extra={"reason": reason})


def get_announcer(
    request: Request, settings: SettingsDep, client: FgaClientDep, resolver: ResolverDep, control: ControlEmitterDep, uuids: ObjectUuidsDep
) -> Announcer:
    """This request's :class:`Announcer`. Its lineage emitter is the app's own, unbound to the request: a marker carries the
    originator its filing request claimed, whichever request sends it. The headers are read off the request, so no door
    gains a documented parameter by announcing."""
    emitter: LineageEmitter | None = getattr(request.app.state, "lineage_emitter", None)
    return Announcer(
        settings,
        client=client if settings.fga_enabled else None,
        resolver=resolver,
        control=control,
        lineage=emitter if emitter is not None else NoopEmitter(),
        uuids=uuids,
        originator=originator_hint(request.headers.get("x-lance-originator")),
        authorization=request.headers.get("authorization"),
    )


AnnouncerDep = Annotated[Announcer, Depends(get_announcer)]
