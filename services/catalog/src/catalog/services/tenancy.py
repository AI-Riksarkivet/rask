"""The ONE resolver of a project's or a warehouse's id to its live creation, and the one comparison of a record's project
(ADR 0245).

An id is the address every door, the console, the declaration and the events name; OpenFGA names the object by the uuid
its creation minted (``lakehouse.governed.tenancy``). A door resolves the id to the uuid of its LIVE record (one registry
read), asks its gate on that object, writes on that object, and resolves the id again once it has written: an id no
longer naming that uuid live was deleted meanwhile, and the door answers what a door on a deleted object answers. A
write that landed meanwhile landed on a dead object nothing resolves to, which authorizes nothing, and the converge tick
sweeps it (``tenancy_converge``).

A record that belongs to a project (a warehouse's, a role's, a bucket's claim, a project's policy) names the uuid of the
project's creation it was made under, and ``lakehouse.governed.tenancy.made_under`` is the one comparison of it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from lance_namespace import ConcurrentModificationError, InternalError, ServiceUnavailableError, TableNotFoundError
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from catalog.services import projects, warehouses
from catalog.services.control_records import (
    DELETE_ASKED,
    DELETE_FIELDS,
    DELETE_RUN,
    DELETING_BY,
    MARKED_AT,
    NO_RETURN,
    DeleteAsked,
    EndedBy,
    NotTheCreation,
    change_creation,
    end_record,
    read_creation,
)
from lakehouse.governed import tenancy
from lakehouse.governed.tenancy import DELETING, LIVE, PROJECT, STATE, UUID, WAREHOUSE, Kind
from lakehouse.records import Stored


if TYPE_CHECKING:
    from catalog.core.config import Settings
    from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: The actor the converge tick's writes are recorded as: no person, the catalog converging an invariant of its own.
ACTOR: Final = "system:catalog"
#: What a create or a claim outlives a request by before the converge tick ends it. A door runs on past its request's
#: deadline (``lakehouse.deadline``), so the margin covers that run.
MARGIN: Final = timedelta(minutes=5)


def create_bound(request_deadline_seconds: float) -> timedelta:
    """How old a `creating` record or a dead creation's claim is before the converge tick ends it: the request deadline
    plus :data:`MARGIN`, so no create still running is ended under it."""
    return timedelta(seconds=request_deadline_seconds) + MARGIN


class Tenant(BaseModel):
    """A project or a warehouse an id resolved to: its live record, and the object OpenFGA names it by."""

    model_config = ConfigDict(frozen=True)

    kind: Kind
    id: str
    #: ``project:<uuid>`` or ``warehouse:<uuid>``; the estate's own warehouse's configured object.
    obj: str
    #: Its registry record; empty for the estate's own warehouse, which no record names.
    record: dict[str, Any]

    @property
    def uuid(self) -> str | None:
        """The uuid its creation minted; ``None`` for the estate's own warehouse."""
        return self.record.get(UUID)


def object_of_record(kind: Kind, record: dict[str, Any]) -> str:
    """The object of the creation ``record`` names: what a reader acting from a record it already holds writes on."""
    return tenancy.object_of(kind, str(record[UUID]))  # noqa: TID251 — the resolver's own builder


#: The states a door resolves an id in: live alone, so nothing is granted or written on a creation being deleted.
DOORS: Final[frozenset[str]] = frozenset({LIVE})
#: The states the data plane routes a warehouse's names in: live, and being deleted, whose namespaces and tables stand
#: until its delete has dropped them, so a delete that stopped part-way leaves them answered as they are. A namespace made
#: under one being deleted is taken back by its create, which resolves the warehouse again for doors once seeded.
SERVED: Final[frozenset[str]] = frozenset({LIVE, DELETING})


def _tenant(kind: Kind, record: dict[str, Any] | None, states: frozenset[str] = DOORS) -> Tenant | None:
    if record is None or record.get(STATE) not in states:
        return None
    return Tenant(kind=kind, id=str(record["id"]), obj=object_of_record(kind, record), record=record)


def resolve(control_root: str, storage_options: StorageOptions, kind: Kind, object_id: str, *, states: frozenset[str] = DOORS) -> Tenant | None:
    """The creation ``object_id`` of ``kind`` names now in one of ``states`` (live, for a door); ``None`` where none
    stands so (absent, still creating, or being deleted). One registry read. Blocking.

    Raises:
        ServiceUnavailableError: The record is there and cannot be read as one.
    """
    read = projects.get_project_record if kind == PROJECT else warehouses.get_warehouse_record
    return _tenant(kind, read(control_root, storage_options, object_id), states)


async def resolved(settings: Settings, kind: Kind, object_id: str, *, states: frozenset[str] = DOORS) -> Tenant | None:
    """:func:`resolve` on the catalog's registry; the estate's own warehouse, which no record names and no door deletes,
    resolves to its configured object."""
    if kind == WAREHOUSE and object_id == settings.estate_warehouse:
        return Tenant(kind=WAREHOUSE, id=object_id, obj=settings.fga_default_warehouse_object, record={})
    return await run_in_threadpool(partial(resolve, settings.registry_root, settings.storage_options(), kind, object_id, states=states))


async def require(settings: Settings, kind: Kind, object_id: str) -> Tenant:
    """:func:`resolved`, or the missing object's 404.

    Raises:
        TableNotFoundError: No live creation of ``kind`` holds ``object_id``.
    """
    if (tenant := await resolved(settings, kind, object_id)) is None:
        raise TableNotFoundError(f"{kind} not found: {object_id}")
    return tenant


async def still(settings: Settings, tenant: Tenant) -> bool:
    """Whether ``tenant``'s id still names its creation live: the second resolve of a door that wrote on it."""
    now = await resolved(settings, tenant.kind, tenant.id)
    return now is not None and now.obj == tenant.obj


async def require_still(settings: Settings, tenant: Tenant) -> None:
    """Answer a door that wrote on ``tenant`` as the deleted object's 404 where its id no longer names it live.

    Raises:
        TableNotFoundError: ``tenant`` was deleted while the door ran; what it wrote stands on a dead object.
    """
    if not await still(settings, tenant):
        raise TableNotFoundError(f"{tenant.kind} not found: {tenant.id} (it was deleted while this request ran)")


async def creation_ended(settings: Settings, tenant: Tenant) -> bool:
    """Whether no record names ``tenant``'s creation now, in any state: its delete ended it. A creation's uuid is never
    minted again, so once none does none ever will, and what a door wrote under it is the door's to take back."""
    stored = await run_in_threadpool(read_creation, settings.registry_root, settings.storage_options(), key_of(tenant.kind, tenant.id))
    return stored is None or stored.record.get(UUID) != tenant.uuid


def key_of(kind: Kind, object_id: str) -> str:
    """Where the record of ``kind`` ``object_id`` lives on the control root."""
    return projects.record_key(object_id) if kind == PROJECT else warehouses.record_key(object_id)


class DeleteGone(TableNotFoundError):
    """A delete stopped: the id no longer names the creation it was asked of (it ended, or another tenant made it again),
    so it ends nothing more. Answered as a door on a deleted object answers (404)."""


class DeleteTakenOver(ConcurrentModificationError):
    """A delete stopped: another delete of the same creation took its mark over, and finishes it (409)."""


class MarkLeft(ServiceUnavailableError):
    """A delete failed before its point of no return and its record could not be put back live either: the object is left
    marked ``deleting``, nothing of it deleted. Asking the same delete again judges it afresh, and the converge tick puts
    it back live once the delete's run is older than its bound (503: asking again is the recovery). The answer says so in
    its extension members, since a 5xx ``detail`` is redacted (``lakehouse.ns_errors.problem_detail``)."""

    def __init__(self, message: str, *, problem_extra: dict[str, object]) -> None:
        super().__init__(message)
        self.problem_extra = problem_extra


class DeleteRun(BaseModel):
    """One delete of one creation, as its request runs it (ADR 0245, reviews T2, T3 and S1): the token its mark carries.

    A delete's mark is written under the ETag of the read its checks judged (:func:`marked`), so a change landing between
    them refuses it, and a delete asked again over another's mark takes the delete over: as a NEW delete, judged on every
    check again, unless the mark it takes over is past its point of no return (:func:`judges`). From the mark to the
    point of no return (:meth:`Marking.pass_no_return`) nothing irreversible is done, so ANY failure there puts the record
    back live (:func:`marked`). Its point of no return names the run and what it was asked (``delete_asked``), so the
    converge tick finishes a delete whose run died past it exactly as asked. Every write past the mark is conditional on
    a read that found this run's mark (:func:`pass_no_return`, :func:`end`), and every step acts on the creation's own
    objects, so a delete held across its creation's end, and the id made again, ends nothing of the creation that holds
    the id now."""

    model_config = ConfigDict(frozen=True)

    kind: Kind
    id: str
    uuid: str
    run: str

    @classmethod
    def of(cls, kind: Kind, record: dict[str, Any]) -> DeleteRun:
        """A new run of a delete of the creation ``record`` names."""
        return cls(kind=kind, id=str(record["id"]), uuid=str(record[UUID]), run=tenancy.new_uuid())

    @property
    def key(self) -> str:
        return key_of(self.kind, self.id)

    def gone(self) -> DeleteGone:
        """What a door on the deleted object answers."""
        return DeleteGone(f"{self.kind} not found: {self.id}")


def judges(record: dict[str, Any]) -> bool:
    """Whether a delete reading ``record`` judges its checks (authorization's own gate aside: every delete is gated):
    unless a delete passed its point of no return on it, when what is left is to finish it. A mark with no stamp is only
    a delete that began, and taking it over is a new delete (``ChecksTakeOver``)."""
    return NO_RETURN not in record


class NothingToFinish(DeleteGone):
    """The converge tick's finish found, on its fresh read, no delete to finish: the record is not the run its listing
    found past its point of no return (it was finished, taken over by a delete asked again, or is another creation's).
    The tick never begins a delete (``FreshFinish``), so it does nothing."""


async def judged(settings: Settings, delete: DeleteRun, *, finishing: str | None = None) -> Stored:
    """The record ``delete`` judges its checks on, with the ETag its mark is conditional on.

    ``finishing`` is the run a converge tick's listing found past its point of no return: then the record read now must
    still be that run's stamped mark, and the mark the tick takes over is conditional on this very read, so a record its
    listing found stamped and that changed since (finished, taken over, another creation's) is no delete it finishes.

    Raises:
        DeleteGone: The id no longer names the creation, or its record is neither live nor marked.
        NothingToFinish: ``finishing`` names a run the record read now does not carry past its point of no return.
    """
    stored = await run_in_threadpool(read_creation, settings.registry_root, settings.storage_options(), delete.key)
    if stored is None or stored.record.get(UUID) != delete.uuid or stored.record.get(STATE) not in SERVED:
        raise delete.gone()
    if finishing is not None and not (stored.record.get(STATE) == DELETING and NO_RETURN in stored.record and stored.record.get(DELETE_RUN) == finishing):
        raise NothingToFinish(f"{delete.kind} {delete.id!r} holds no stamped mark of the delete run {finishing}; nothing to finish")
    return stored


def asked_of(record: dict[str, Any]) -> DeleteAsked:
    """What the delete that passed ``record``'s point of no return was asked to do, as its stamp records it: what a delete
    asked again over it, or the converge tick, finishes, whatever its own caller asks."""
    return DeleteAsked.model_validate(record.get(DELETE_ASKED) or {})


class Stamp(BaseModel):
    """A delete's point of no return as its record carries it: the run that passed it, which names the delete from then on
    whichever request or tick finishes it, and what that run was asked."""

    model_config = ConfigDict(frozen=True)

    run: str
    asked: DeleteAsked

    def ended(self, *, bucket_purged: bool | None = None) -> EndedBy:
        """The delete as the record of the creation it ends names it."""
        return EndedBy(no_return=self.run, asked=self.asked, bucket_purged=bucket_purged)


def stamp_of(record: dict[str, Any]) -> Stamp | None:
    """``record``'s point of no return; ``None`` where no delete passed one on it."""
    run = record.get(NO_RETURN)
    return Stamp(run=str(run), asked=asked_of(record)) if run else None


async def _lost(settings: Settings, delete: DeleteRun) -> DeleteGone | DeleteTakenOver:
    """Why a write of ``delete`` conditional on its own mark did not land: the creation went (404), or its mark is no
    longer this run's (409: another delete took it over, or the converge tick settled it once the run passed its bound),
    while the creation stands."""
    stored = await run_in_threadpool(read_creation, settings.registry_root, settings.storage_options(), delete.key)
    record = stored.record if stored is not None else None
    if record is None or record.get(UUID) != delete.uuid:
        return delete.gone()
    return DeleteTakenOver(
        f"the mark of this delete of {delete.kind} {delete.id!r} is no longer its own: another delete took it over, or the converge tick settled it; ask again"
    )


class Marking:
    """A delete's span from its mark to its point of no return (:func:`marked`)."""

    def __init__(self, settings: Settings, delete: DeleteRun) -> None:
        self._settings, self._delete = settings, delete
        self.stamp: Stamp | None = None

    @property
    def stamped(self) -> bool:
        return self.stamp is not None

    async def pass_no_return(self, asked: DeleteAsked | None = None) -> Stamp:
        """Stamp the delete's point of no return, the span's last step (:func:`pass_no_return`); the stamp as it stands."""
        self.stamp = await pass_no_return(self._settings, self._delete, asked)
        return self.stamp


@asynccontextmanager
async def marked(settings: Settings, delete: DeleteRun, read: Stored, *, by: str) -> AsyncGenerator[Marking]:
    """Mark the record ``deleting`` by ``delete`` under the ETag of ``read`` (:func:`mark`), and hold the span to the
    delete's point of no return, which the block ends with (:meth:`Marking.pass_no_return`).

    ONE way for both delete doors, so no path out of the span skips it: whatever the block raises before the stamp
    landed (a refusal, a store or OpenFGA failing, the stamp's own write failing) puts the record back live, since
    nothing irreversible has been done; a delete that lost its mark meanwhile (taken over, or its creation gone) leaves
    it to whoever holds it (:func:`put_back` puts back only this run's own mark). Where the put-back fails too, the answer
    says the object is left marked and how it is cleared (:class:`MarkLeft`). A delete taking over a mark already past its
    point of no return (``read`` carries the stamp) puts nothing back whatever the block raises: past the stamp the delete
    only finishes (``NoReturnFinal``).

    Raises:
        ConcurrentModificationError: The record changed since ``read``; nothing was marked.
        MarkLeft: The block failed and the record could not be put back.
        InternalError: The block ended without passing the point of no return.
    """
    await mark(settings, delete, read, by=by)
    marking = Marking(settings, delete)
    try:
        yield marking
    except (DeleteGone, DeleteTakenOver):
        raise
    except BaseException as exc:
        if not marking.stamped and judges(read.record):
            try:
                await put_back(settings, delete)
            except Exception as failed:
                log.error("delete_mark_left", extra={"kind": delete.kind, "id": delete.id, "uuid": delete.uuid, "error": str(failed)[:300]})
                cleared = (
                    f"ask the same delete again, or the catalog's converge tick puts it back live within {create_bound(settings.request_deadline_seconds)}"
                )
                raise MarkLeft(
                    f"{delete.kind} {delete.id!r} is left marked for deletion, nothing of it deleted: its delete failed before its point of no "
                    f"return and putting it back live failed too; {cleared}.",
                    problem_extra={delete.kind: delete.id, "marked_for_deletion": True, "deleted": False, "cleared_by": cleared},
                ) from exc
        raise
    if not marking.stamped:
        raise InternalError(f"the delete of {delete.kind} {delete.id!r} left its span without passing its point of no return")


async def mark(settings: Settings, delete: DeleteRun, read: Stored, *, by: str) -> dict[str, Any]:
    """Mark the record ``deleting`` by ``delete``, under the ETag of ``read``: from here no door resolves it, and the
    first caller to start a delete stays named as its starter. Asked through :func:`marked`.

    Raises:
        ConcurrentModificationError: The record changed since ``read`` (another delete's mark, a protection change): judged
            on a record that is no longer there, the delete changes nothing; asked again, it judges the one there now.
    """
    record = read.record
    marked = {**record, STATE: DELETING, DELETING_BY: record.get(DELETING_BY) or by, DELETE_RUN: delete.run, MARKED_AT: datetime.now(UTC).isoformat()}
    write = projects.mark_deleting if delete.kind == PROJECT else warehouses.mark_deleting
    if not await run_in_threadpool(partial(write, settings.registry_root, settings.storage_options(), delete.id, read, marked)):
        raise ConcurrentModificationError(f"{delete.kind} {delete.id!r} changed while its delete was judged; nothing was deleted, ask again")
    return marked


async def pass_no_return(settings: Settings, delete: DeleteRun, asked: DeleteAsked | None = None) -> Stamp:
    """Stamp ``delete``'s point of no return on its record, right before its first irreversible step, under the ETag of a
    read that found its own mark: past it nothing puts the record back (:func:`put_back`), and a failure leaves it marked
    for the same delete asked again, or the converge tick, to finish as ``asked``. The stamp is written once, by the first
    run past it, naming that run and what it was asked: a delete taking over a stamped mark keeps both (:func:`asked_of`),
    whatever its own caller asked, so a resumed delete never widens or narrows what the delete that passed the stamp was
    authorized and judged for, and every run finishing it purges under the one name (``Grants.tla`` U21, U23). The stamp
    as it stands.

    Raises:
        DeleteGone: The id no longer names the creation.
        DeleteTakenOver: Another delete of it took its mark over.
    """

    def stamp(record: dict[str, Any]) -> dict[str, Any]:
        if record.get(DELETE_RUN) != delete.run:
            raise NotTheCreation(f"{delete.key} is marked by another delete")
        if NO_RETURN in record:
            return record
        return {**record, NO_RETURN: delete.run, DELETE_ASKED: (asked or DeleteAsked()).model_dump()}

    change = partial(change_creation, settings.registry_root, settings.storage_options(), delete.key, delete.uuid, stamp, states=frozenset({DELETING}))
    if (stamped := await run_in_threadpool(change)) is None or (passed := stamp_of(stamped)) is None:
        raise await _lost(settings, delete)
    return passed


def _back_live(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in DELETE_FIELDS} | {STATE: LIVE}


async def put_back(settings: Settings, delete: DeleteRun) -> bool:
    """Put the record back ``live`` after ``delete`` stopped short of its point of no return, where nothing irreversible
    was done to the creation; whether it was put back. Only the delete whose run holds the mark puts it back
    (``PutsBackOwnMark``): a mark another delete took over since is that delete's, which goes on unaware of this one. A
    record past any point of no return is never put back (``NoReturnFinal``): every step that can refuse runs before the
    stamp, and past it the delete only finishes, by its door asked again or by the converge tick."""

    def live(record: dict[str, Any]) -> dict[str, Any]:
        if record.get(DELETE_RUN) != delete.run:
            raise NotTheCreation(f"{delete.key} is marked by another delete")
        if NO_RETURN in record:
            raise NotTheCreation(f"{delete.key} is past its point of no return")
        return _back_live(record)

    change = partial(change_creation, settings.registry_root, settings.storage_options(), delete.key, delete.uuid, live, states=frozenset({DELETING}))
    return await run_in_threadpool(change) is not None


def put_back_stale(control_root: str, storage_options: StorageOptions, kind: Kind, record: dict[str, Any], *, older_than: datetime) -> bool:
    """The converge tick's put-back (``TickSettlesMarks``): ``record``'s mark, carrying no point of no return and written
    before ``older_than`` (its run no longer runs), put back live, under the ETag of a read that finds that same run's
    mark so. Whether it was. Blocking."""
    run = record.get(DELETE_RUN)

    def live(current: dict[str, Any]) -> dict[str, Any]:
        if current.get(DELETE_RUN) != run or NO_RETURN in current or not marked_before(current, older_than):
            raise NotTheCreation(f"{record['id']} holds another mark, or one within the bound")
        return _back_live(current)

    key = key_of(kind, str(record["id"]))
    return change_creation(control_root, storage_options, key, str(record[UUID]), live, states=frozenset({DELETING})) is not None


def marked_before(record: dict[str, Any], older_than: datetime) -> bool:
    """Whether ``record``'s mark was written before ``older_than``, so its run no longer runs; a mark naming no time it was
    written was written by no run this catalog is running, and is judged so."""
    stamped = record.get(MARKED_AT)
    try:
        return not isinstance(stamped, str) or datetime.fromisoformat(stamped) < older_than
    except ValueError:
        return True


async def end(settings: Settings, delete: DeleteRun, by: EndedBy) -> None:
    """End ``delete``'s creation: kept under ``_ended/`` naming the delete ``by`` names, and its record removed under the
    ETag of a read that found this run's mark (``control_records.end_record``), which frees its id.

    Raises:
        DeleteGone: The id no longer names the creation.
        DeleteTakenOver: Another delete of it took its mark over.
    """
    ending = partial(
        end_record,
        settings.registry_root,
        settings.storage_options(),
        delete.kind,
        delete.key,
        delete.id,
        delete.uuid,
        run=delete.run,
        by=by,
        now=datetime.now(UTC),
    )
    if not await run_in_threadpool(ending):
        raise await _lost(settings, delete)


def ids_by_object(kind: Kind, records: Iterable[dict[str, Any]], *, states: frozenset[str] = DOORS) -> dict[str, str]:
    """Each record's object in one of ``states`` (live, by default), to its id: what a listing that asked OpenFGA maps its
    answer back to ids by."""
    return {object_of_record(kind, r): str(r["id"]) for r in records if r.get(STATE) in states}


#: Every state a record stands in until its delete ends it: what the estate admin's tools name a creation in.
STANDING: Final[frozenset[str]] = frozenset({tenancy.CREATING, LIVE, DELETING})


def nowhere(kind: Kind) -> str:
    """An object of ``kind`` no creation is named by: what a gate is asked on for an id no live creation holds, so the
    caller is answered as for an object it holds nothing on and learns nothing about which ids exist."""
    return tenancy.object_of(kind, "0" * 32)  # noqa: TID251 — the resolver's own builder
