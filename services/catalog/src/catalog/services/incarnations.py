"""Every project and warehouse record names an incarnation, every incarnation is born, and what a request or a tenant
that died left behind is ended (ADR 0241).

A project's or warehouse's id may be held again by another tenant once it is deleted, so each write of a tuple on or
naming it is fenced by the incarnation its record names and its ``revision`` token in OpenFGA (``fga.fenced``), and a
record that names a parent which can be deleted and made again (a warehouse's project, a role's project, a bucket's
claim) names the incarnation of the parent it was made under (:data:`PROJECT_INCARNATION`).

A create mints its record's incarnation with the record, so every record names one: one that names none was not written
by this catalog, and is a typed internal error naming it (:func:`require_incarnation`), never repaired. An estate whose
records predate incarnations is re-created from git (ADR 0241 §8 B5). What stays is a real invariant: a create can die
between its record and its birth, so its incarnation is unborn; its doors answer 409 with a ``Retry-After`` meanwhile
(:func:`establishing`), and the convergence bears it with no seed of its own.

The convergence runs at boot and on each converge tick (``main.converge_at_boot``, ``declaration_converge.tick``), so
nothing is hand-run: it bears every unborn incarnation of a live tenant; it sweeps every fence ticket older than
:func:`ticket_bound` on each id a record names (a ticket a request that died left, ``fga.sweep_tickets``); and it reports
every record a dead tenant left (a warehouse or role whose project incarnation no longer stands, written by a create
that raced its project's delete), refuses to bear or write any edge from it, and ends it once its create cannot still
be running: its record goes and its name is free. An ended warehouse's bucket, its bytes and its claim are kept (the
claim names the dead incarnation, so no tenant registers the bucket over them) and reported: an unborn warehouse held no
grant, so no door made anything in it, and no data is deleted on a guess.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from lance_namespace import InternalError
from opentelemetry import metrics
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from catalog.services import projects, roles, warehouses
from catalog.services.control_records import INCARNATION, PROJECT_INCARNATION
from lakehouse.governed import fga
from lakehouse.ns_errors import SettlingError


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient

    from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

_orphans = metrics.get_meter("lance.catalog").create_counter(
    "catalog.incarnations.orphans",
    unit="1",
    description="Records a dead tenant's create left (a warehouse or role naming a project incarnation that no longer stands), each time one is found.",
)

#: The actor the convergence's writes are recorded as: no person, the catalog converging an invariant of its own.
ACTOR: Final = "system:catalog"
#: Seconds a door asks its caller to wait for a record still being established: the convergence runs at boot and on the
#: next converge tick, and a create still running bears its record within one request.
RETRY_AFTER_SECONDS: Final = 5
#: What a ticket or an orphan record outlives a request by before the convergence ends it. A door runs on past its
#: request's deadline (``lakehouse.deadline``), so the margin covers that run; a door still running past the bound has its
#: ticket deleted and its write refused 409, narrower, never wider.
MARGIN: Final = timedelta(minutes=5)


def ticket_bound(request_deadline_seconds: float) -> timedelta:
    """How old a ticket or an orphan record is before the convergence ends it: the request deadline plus :data:`MARGIN`."""
    return timedelta(seconds=request_deadline_seconds) + MARGIN


class IncarnationsConverged(BaseModel):
    """What one convergence did, each object as ``project:<id>`` / ``warehouse:<id>`` / ``role:<uuid>``."""

    #: Incarnations born.
    born: list[str] = Field(default_factory=list)
    #: Tickets swept, by the object they named.
    swept: dict[str, int] = Field(default_factory=dict)
    #: Records a dead tenant left, found and refused.
    orphans: list[str] = Field(default_factory=list)
    #: Of those, the ones ended (their records removed).
    ended: list[str] = Field(default_factory=list)
    #: Objects that could not be converged, with why; the next run tries again.
    failed: list[str] = Field(default_factory=list)


def require_incarnation(record: dict[str, Any], obj: str) -> str:
    """The incarnation ``record`` names, which every project and warehouse record carries from its create.

    Raises:
        InternalError: The record names none: no create of this catalog wrote it (ADR 0241 §8 B5).
    """
    if not (incarnation := record.get(INCARNATION)):
        log.error("record_without_incarnation", extra={"object": obj})
        raise InternalError(f"{obj}'s record names no incarnation; every create mints one, so this record was not written by this catalog")
    return str(incarnation)


def require_project_incarnation(record: dict[str, Any], obj: str) -> str:
    """The incarnation of its project ``record`` (a warehouse's) names, which every warehouse record carries from its
    create.

    Raises:
        InternalError: The record names none: no create of this catalog wrote it (ADR 0241 §8 B5).
    """
    if not (incarnation := record.get(PROJECT_INCARNATION)):
        log.error("record_without_project_incarnation", extra={"object": obj})
        raise InternalError(f"{obj}'s record names no incarnation of its project; every create names one, so this record was not written by this catalog")
    return str(incarnation)


def establishing(obj: str) -> SettlingError:
    """The refusal of a door acting on ``obj``, whose record stands and whose incarnation is not yet born."""
    return SettlingError(f"{obj} is still being established; ask again shortly", retry_after=RETRY_AFTER_SECONDS)


def report_orphan(obj: str, *, project: str, project_incarnation: str) -> None:
    """Report a record a dead tenant's create left: ``obj`` names ``project`` at an incarnation that no longer stands."""
    _orphans.add(1, {"lance.catalog.object_type": obj.partition(":")[0]})
    log.error("record_of_a_dead_tenant", extra={"object": obj, "project": project, "project_incarnation": project_incarnation})


def project_incarnation(control_root: str, storage_options: StorageOptions, project: str) -> str | None:
    """The incarnation the record of ``project`` names, its delete begun or not; ``None`` where no record stands.

    Raises:
        InternalError: The record names no incarnation.
    """
    record = projects.get_project_record(control_root, storage_options, project)
    return None if record is None else require_incarnation(record, f"project:{project}")


def _written(stamp: str | datetime | None) -> datetime | None:
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(stamp).astimezone(UTC) if stamp else None
    except ValueError:
        return None


class _Convergence:
    """One run of the convergence over the registry and OpenFGA."""

    def __init__(self, control_root: str, storage_options: StorageOptions, client: OpenFgaClient | None, *, bound: timedelta, now: datetime) -> None:
        self._root, self._so, self._client = control_root, storage_options, client
        self._older_than = now - bound
        self._may_end = False
        self.done = IncarnationsConverged()

    async def run(self) -> IncarnationsConverged:
        records, unreadable = await run_in_threadpool(projects.read_projects, self._root, self._so)
        # A project record that cannot be read may name the incarnation a warehouse or role was made under, so nothing is
        # judged a dead tenant's while one cannot: orphans are reported and kept.
        self._may_end = not unreadable
        live: dict[str, str] = {}
        for record in records:
            obj = f"project:{record['id']}"
            try:
                live[str(record["id"])] = require_incarnation(record, obj)
            except InternalError as exc:
                self._may_end = False
                self.done.failed.append(f"{obj}: {exc}")
        deleting = {str(record["id"]) for record in records if record.get(projects.DELETING_BY)}
        for project, incarnation in live.items():
            await self._attempt(
                f"project:{project}", lambda obj=f"project:{project}", inc=incarnation, bear=project not in deleting: self._incarnated(obj, inc, bear=bear)
            )
            for role in await run_in_threadpool(partial(roles.orphan_roles, self._root, self._so, project, incarnation=incarnation)):
                await self._attempt(roles.role_object(role.id), lambda role=role: self._orphan_role(role))
        readable, unreadable_warehouses = await run_in_threadpool(warehouses.read_warehouses, self._root, self._so)
        if unreadable_warehouses:
            log.warning("incarnation_converge_skipped_unreadable", extra={"warehouses": unreadable_warehouses})
        for record in readable:
            if record.get("id"):
                await self._attempt(f"warehouse:{record['id']}", lambda record=record: self._warehouse(record, live))
        if self.done.born or self.done.swept or self.done.orphans or self.done.failed:
            log.info("incarnations_converged", extra=self.done.model_dump())
        return self.done

    async def _attempt(self, obj: str, step: Any) -> None:  # noqa: ANN401 — a zero-argument coroutine factory
        try:
            await step()
        except Exception as exc:  # noqa: BLE001 — one object that cannot be converged must not stop the rest; the next run retries it
            log.warning("incarnation_not_converged", extra={"object": obj, "error": str(exc)[:300]})
            self.done.failed.append(f"{obj}: {exc}")

    async def _incarnated(self, obj: str, incarnation: str, *, bear: bool = True) -> None:
        """Bear ``obj``'s unborn ``incarnation`` with no seed of its own, unless its delete has begun, and sweep the tickets
        past the bound."""
        if self._client is None:
            return
        if bear and await fga.bear(self._client, obj, incarnation, [], actor=ACTOR, origin="registry_rebuild"):  # noqa: TID251
            self.done.born.append(obj)
        if swept := await fga.sweep_tickets(self._client, obj, older_than=self._older_than):
            self.done.swept[obj] = swept

    async def _warehouse(self, record: dict[str, str], live: dict[str, str]) -> None:
        obj = f"warehouse:{record['id']}"
        incarnation = require_incarnation(record, obj)
        project_incarnation = require_project_incarnation(record, obj)
        if live.get(record["project"]) == project_incarnation:
            await self._incarnated(obj, incarnation)
            return
        report_orphan(obj, project=record["project"], project_incarnation=project_incarnation)
        self.done.orphans.append(obj)
        # With authorization off no birth tells whether a door made anything in it, and once born it may hold grants and
        # data a door made: either way it is reported and kept.
        if self._client is None or await fga.is_born(self._client, obj, incarnation):
            return
        await fga.sweep_tickets(self._client, obj, older_than=self._older_than)
        if not self._may_end or not self._past_bound(record.get("created_at")):
            return
        await run_in_threadpool(warehouses.delete_warehouse_record, self._root, self._so, str(record["id"]))
        log.warning(
            "warehouse_of_a_dead_tenant_ended",
            extra={"warehouse": record["id"], "project": record["project"], "bucket": record.get("bucket"), "kept": "bucket, bytes, claim"},
        )
        self.done.ended.append(obj)

    def _past_bound(self, created: str | datetime | None) -> bool:
        """Whether a record made at ``created`` is older than the bound, so the create that wrote it no longer runs."""
        written = _written(created)
        return written is not None and written < self._older_than

    async def _orphan_role(self, role: roles.StoredRole) -> None:
        obj = roles.role_object(role.id)
        report_orphan(obj, project=role.project, project_incarnation=role.project_incarnation)
        self.done.orphans.append(obj)
        if self._client is None or not self._may_end or not self._past_bound(role.created_at):
            return
        # Its create died after the project's delete drained its edge and ended the roles it listed: it holds no edge and
        # no assignee, so only its record is ended, and its name is free under the project.
        await run_in_threadpool(roles.end_role_record, self._root, self._so, role)
        self.done.ended.append(obj)


async def converge(
    control_root: str, storage_options: StorageOptions, client: OpenFgaClient | None, *, bound: timedelta, now: datetime | None = None
) -> IncarnationsConverged:
    """Bear every unborn incarnation of a live tenant, sweep the tickets older than ``bound``, and report and end what a
    dead tenant left; what was done.

    Idempotent and safe beside every door: a birth commits only while its marker is absent, so a create still running and
    this both bearing one incarnation make one birth, and the create then writes its seed under a fence; a ticket or an
    orphan is ended only past ``bound``, which a request's run does not reach. With authorization off nothing is born,
    swept or ended, and an orphan is only reported.
    """
    return await _Convergence(control_root, storage_options, client, bound=bound, now=now or datetime.now(UTC)).run()
