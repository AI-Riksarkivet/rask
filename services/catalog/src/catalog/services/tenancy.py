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

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from lance_namespace import ConcurrentModificationError, TableNotFoundError
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from catalog.services import projects, warehouses
from catalog.services.control_records import DELETE_RUN, DELETING_BY, NO_RETURN, NotTheCreation, change_creation, end_record, read_creation
from lakehouse.governed import tenancy
from lakehouse.governed.tenancy import DELETING, LIVE, PROJECT, STATE, UUID, WAREHOUSE, Kind
from lakehouse.records import Stored


if TYPE_CHECKING:
    from catalog.core.config import Settings
    from lakehouse.storage_options import StorageOptions


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


def key_of(kind: Kind, object_id: str) -> str:
    """Where the record of ``kind`` ``object_id`` lives on the control root."""
    return projects.record_key(object_id) if kind == PROJECT else warehouses.record_key(object_id)


class DeleteGone(TableNotFoundError):
    """A delete stopped: the id no longer names the creation it was asked of (it ended, or another tenant made it again),
    so it ends nothing more. Answered as a door on a deleted object answers (404)."""


class DeleteTakenOver(ConcurrentModificationError):
    """A delete stopped: another delete of the same creation took its mark over, and finishes it (409)."""


class DeleteRun(BaseModel):
    """One delete of one creation, as its request runs it (ADR 0245, review T2 and T3): the token its mark carries.

    A delete's mark is written under the ETag of the read its checks judged (:func:`mark`), so a change landing between
    them refuses it, and a delete asked again over another's mark takes the delete over. Every step after the mark acts
    only while the record still names this creation marked by this run (:func:`require_own`), so a delete held across
    its creation's end, and the id made again, ends nothing of the creation that holds the id now. Its point of no return
    is stamped before its first irreversible step (:func:`pass_no_return`); a refusal before it puts the record back live
    (:func:`put_back`), and its end is the record's one removal (:func:`end`)."""

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


async def judged(settings: Settings, delete: DeleteRun) -> Stored:
    """The record ``delete`` judges its checks on, with the ETag its mark is conditional on.

    Raises:
        DeleteGone: The id no longer names the creation, or its record is neither live nor marked.
    """
    stored = await run_in_threadpool(read_creation, settings.registry_root, settings.storage_options(), delete.key)
    if stored is None or stored.record.get(UUID) != delete.uuid or stored.record.get(STATE) not in SERVED:
        raise delete.gone()
    return stored


async def mark(settings: Settings, delete: DeleteRun, read: Stored, *, by: str) -> dict[str, Any]:
    """Mark the record ``deleting`` by ``delete``, under the ETag of ``read``: from here no door resolves it, and the
    first caller to start a delete stays named as its starter.

    Raises:
        ConcurrentModificationError: The record changed since ``read`` (another delete's mark, a protection change): judged
            on a record that is no longer there, the delete changes nothing; asked again, it judges the one there now.
    """
    record = read.record
    marked = {**record, STATE: DELETING, DELETING_BY: record.get(DELETING_BY) or by, DELETE_RUN: delete.run}
    write = projects.mark_deleting if delete.kind == PROJECT else warehouses.mark_deleting
    if not await run_in_threadpool(partial(write, settings.registry_root, settings.storage_options(), delete.id, read, marked)):
        raise ConcurrentModificationError(f"{delete.kind} {delete.id!r} changed while its delete was judged; nothing was deleted, ask again")
    return marked


async def require_own(settings: Settings, delete: DeleteRun) -> dict[str, Any]:
    """The record, where it still names ``delete``'s creation marked by ``delete``: asked after each step of a delete that
    can take long.

    Raises:
        DeleteGone: The id no longer names the creation (it ended, or another tenant made it again).
        DeleteTakenOver: Another delete of it took its mark over, and finishes it.
    """
    stored = await run_in_threadpool(read_creation, settings.registry_root, settings.storage_options(), delete.key)
    record = stored.record if stored is not None else None
    if record is None or record.get(UUID) != delete.uuid or record.get(STATE) != DELETING:
        raise delete.gone()
    if record.get(DELETE_RUN) != delete.run:
        raise DeleteTakenOver(f"another delete of {delete.kind} {delete.id!r} took it over; it finishes the delete")
    return record


def _own(delete: DeleteRun, change: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def guarded(record: dict[str, Any]) -> dict[str, Any]:
        if record.get(DELETE_RUN) != delete.run:
            raise NotTheCreation(f"{delete.key} is marked by another delete")
        return change(record)

    return guarded


async def pass_no_return(settings: Settings, delete: DeleteRun) -> None:
    """Stamp ``delete``'s point of no return on its record, right before its first irreversible step: past it a failure
    leaves the record marked, and the same delete asked again finishes it. The stamp names the run, so a guard of this run
    refusing before any step landed may still put it back (:func:`put_back`).

    Raises:
        DeleteGone: The id no longer names the creation.
        DeleteTakenOver: Another delete of it took its mark over.
    """
    stamp = _own(delete, lambda record: {**record, NO_RETURN: delete.run})
    change = partial(change_creation, settings.registry_root, settings.storage_options(), delete.key, delete.uuid, stamp, states=frozenset({DELETING}))
    if await run_in_threadpool(change) is None:
        await require_own(settings, delete)
        raise delete.gone()


async def put_back(settings: Settings, delete: DeleteRun) -> bool:
    """Put the record back ``live`` after a refusal of ``delete`` where nothing irreversible was done to the creation: no
    delete of it passed its point of no return, or only ``delete`` did, which still holds its mark and refused by a guard
    before any step of it landed. Whether it was put back. A record another delete of it has taken past its point of no
    return stays marked, for that delete to finish."""

    def live(record: dict[str, Any]) -> dict[str, Any]:
        if record.get(NO_RETURN) not in (None, delete.run) or (record.get(NO_RETURN) and record.get(DELETE_RUN) != delete.run):
            raise NotTheCreation(f"{delete.key} is past another delete's point of no return")
        return {k: v for k, v in record.items() if k not in (DELETING_BY, DELETE_RUN, NO_RETURN)} | {STATE: LIVE}

    change = partial(change_creation, settings.registry_root, settings.storage_options(), delete.key, delete.uuid, live, states=frozenset({DELETING}))
    return await run_in_threadpool(change) is not None


async def end(settings: Settings, delete: DeleteRun) -> None:
    """End ``delete``'s creation: its record stamped ``ended`` while marked by ``delete``, kept under ``_ended/``, and
    removed (``control_records.end_record``), which frees its id.

    Raises:
        DeleteGone: The id no longer names the creation.
        DeleteTakenOver: Another delete of it took its mark over.
    """
    ending = partial(
        end_record, settings.registry_root, settings.storage_options(), delete.kind, delete.key, delete.uuid, run=delete.run, now=datetime.now(UTC)
    )
    if not await run_in_threadpool(ending):
        await require_own(settings, delete)
        raise delete.gone()


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
