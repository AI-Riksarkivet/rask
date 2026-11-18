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

from collections.abc import Iterable
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from lance_namespace import TableNotFoundError
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from catalog.services import projects, warehouses
from lakehouse.governed import tenancy
from lakehouse.governed.tenancy import DELETING, LIVE, PROJECT, STATE, UUID, WAREHOUSE, Kind


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


def ids_by_object(kind: Kind, records: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Each live record's object, to its id: what a listing that asked OpenFGA maps its answer back to ids by."""
    return {object_of_record(kind, r): str(r["id"]) for r in records if r.get(STATE) == LIVE}


def nowhere(kind: Kind) -> str:
    """An object of ``kind`` no creation is named by: what a gate is asked on for an id no live creation holds, so the
    caller is answered as for an object it holds nothing on and learns nothing about which ids exist."""
    return tenancy.object_of(kind, "0" * 32)  # noqa: TID251 — the resolver's own builder
