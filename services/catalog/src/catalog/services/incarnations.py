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
nothing is hand-run, and its OpenFGA cost is what needs work, never one request per record (ADR 0241 §9 P5): it bears
every unborn incarnation of a live tenant and stamps a record found born (:data:`BORN`), so no later run asks about it;
it sweeps every fence ticket of the estate older than :func:`ticket_bound` (a ticket a request that died left), from one
read of the open tickets (``fga.sweep_tickets``); and it reports every record a dead tenant left (a warehouse, a role or a
project policy naming a project incarnation that no longer stands, written by a create or a set that raced its project's
delete), refuses to bear or write any edge from it, and, once its create cannot still be running, ends an unborn one: a
warehouse's record is stamped ended and kept for the estate admin, which frees its id, and its bucket's claim is released
where the bucket holds only what a create writes (``warehouses.written_by_others``): an unborn warehouse held no grant,
so no door made anything in it, and no byte is deleted or released on a guess. A born orphan is kept, its claim with it,
until the estate admin purges it (ADR 0241 §9 P3).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from lance_namespace import InternalError
from opentelemetry import metrics
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from catalog.services import projects, roles, warehouses
from catalog.services.control_records import BORN, INCARNATION, PROJECT_INCARNATION
from lakehouse import maintenance_policies
from lakehouse.governed import fga
from lakehouse.ns_errors import SettlingError
from lakehouse.records import RecordMissingError, mutate_json


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
    #: Records a dead tenant left, found and refused, an ended warehouse record among them until the estate admin purges
    #: it.
    orphans: list[str] = Field(default_factory=list)
    #: Of those, the ones ended (a warehouse's and a role's record stamped ended).
    ended: list[str] = Field(default_factory=list)
    #: The buckets whose claim a dead incarnation took was released (ADR 0241 §9 P3).
    released: list[str] = Field(default_factory=list)
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
    """The incarnation of its project ``record`` (a warehouse's, a bucket's claim, a project's policy) names, which every
    such record carries from its write.

    Raises:
        InternalError: The record names none: no create of this catalog wrote it (ADR 0241 §8 B5).
    """
    if not (incarnation := record.get(PROJECT_INCARNATION)):
        log.error("record_without_project_incarnation", extra={"object": obj})
        raise InternalError(f"{obj}'s record names no incarnation of its project; every create names one, so this record was not written by this catalog")
    return str(incarnation)


def made_under(record: dict[str, Any], incarnation: str | None, *, obj: str) -> bool:
    """Whether ``record`` (a warehouse's, a bucket's claim, a project's policy) was made under ``incarnation`` of the
    project it names, ``None`` where no incarnation of it stands. The ONE comparison of a record's project incarnation
    (ADR 0241 §9 P1): a record a create that raced its project's delete left names another and is the dead tenant's.

    Raises:
        InternalError: The record names no incarnation of its project (:func:`require_project_incarnation`); it is
            never read as another tenant's, which would hide it from the project it names.
    """
    return require_project_incarnation(record, obj) == incarnation


def warehouses_of(records: Iterable[dict[str, Any]], project: str, incarnation: str | None) -> list[dict[str, Any]]:
    """The warehouse records among ``records`` made under ``incarnation`` of ``project`` (:func:`made_under`): the ONE
    answer to which warehouses a project's standing incarnation holds, which every reader of a project's warehouses asks
    (its listing, its delete's emptiness, its policy's buckets, the purge's siblings).

    Raises:
        InternalError: A record names ``project`` and no incarnation of it.
    """
    return [r for r in records if r.get("project") == project and made_under(r, incarnation, obj=f"warehouse:{r.get('id')}")]


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


#: Why the converge tick ended a warehouse record, and why it released a bucket's claim (ADR 0241 §9 P3).
ENDED_UNBORN: Final = "made_under_a_dead_incarnation"
RELEASED_UNBORN: Final = "claimed_by_a_dead_incarnation"


class _Convergence:
    """One run of the convergence over the registry and OpenFGA: ONE listing of each registry (projects, warehouses,
    roles, claims, policies), and OpenFGA requests only for what needs work (an unborn incarnation, an orphan, an open
    ticket), never one per record (ADR 0241 §9 P5)."""

    def __init__(self, control_root: str, storage_options: StorageOptions, client: OpenFgaClient | None, *, bound: timedelta, now: datetime) -> None:
        self._root, self._so, self._client = control_root, storage_options, client
        self._older_than = now - bound
        self._may_end = False
        self._may_release = False
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
        for record in records:
            project = str(record["id"])
            if project in live and record.get(BORN) != live[project] and not record.get(projects.DELETING_BY):
                await self._attempt(f"project:{project}", partial(self._bear, f"project:{project}", live[project], projects.record_key(project)))
        stored, unreadable_warehouses = await run_in_threadpool(partial(warehouses.read_warehouses, self._root, self._so, ended=True))
        if unreadable_warehouses:
            log.warning("incarnation_converge_skipped_unreadable", extra={"warehouses": unreadable_warehouses})
        # A warehouse record that cannot be read may name a bucket whose claim the tick would release.
        self._may_release = self._may_end and not unreadable_warehouses
        ended: set[str] = set()
        for record in stored:
            obj = f"warehouse:{record['id']}"
            if record.get(warehouses.ENDED_AT):
                # Ended and kept for the estate admin's purge, reported until then (ADR 0241 §9 P3).
                report_orphan(obj, project=str(record["project"]), project_incarnation=str(record.get(PROJECT_INCARNATION)))
                self.done.orphans.append(obj)
            else:
                await self._attempt(obj, partial(self._warehouse, record, live, ended))
        for role in await run_in_threadpool(roles.every_stored_role, self._root, self._so):
            if role.project_incarnation != live.get(role.project):
                await self._attempt(roles.role_object(role.id), partial(self._orphan_role, role))
        claims = await run_in_threadpool(warehouses.every_claim, self._root, self._so)
        standing = [r for r in stored if not r.get(warehouses.ENDED_AT) and r["id"] not in ended]
        for claim in claims:
            await self._attempt(f"bucket:{claim['bucket']}", partial(self._claim, claim, live, standing))
        await self._attempt("policies", partial(self._policies, live))
        if self._client is not None:
            await self._attempt("tickets", self._sweep)
        if self.done.born or self.done.swept or self.done.orphans or self.done.released or self.done.failed:
            log.info("incarnations_converged", extra=self.done.model_dump())
        return self.done

    async def _attempt(self, obj: str, step: Callable[[], Awaitable[None]]) -> None:
        try:
            await step()
        except Exception as exc:  # noqa: BLE001 — one object that cannot be converged must not stop the rest; the next run retries it
            log.warning("incarnation_not_converged", extra={"object": obj, "error": str(exc)[:300]})
            self.done.failed.append(f"{obj}: {exc}")

    async def _bear(self, obj: str, incarnation: str, key: str, fences: Sequence[fga.Fence] = ()) -> None:
        """Bear ``obj``'s ``incarnation`` with no seed of its own, under ``fences``, and remember on its record (``key``)
        that it is born, so no later run asks about it: a marker is never revoked (``fga.bear``), so a record stamped
        born is born. A record stamped born whose marker is absent (a stamp written past every door) is never borne by
        this, and every door on it reads its incarnation from OpenFGA and refuses it 409 until its create is asked again:
        narrower, never wider."""
        if self._client is None:
            return
        if await fga.bear(self._client, obj, incarnation, [], fences=fences, actor=ACTOR, origin="registry_rebuild"):  # noqa: TID251
            self.done.born.append(obj)
        await run_in_threadpool(partial(_stamp_born, self._root, self._so, key, incarnation))

    async def _warehouse(self, record: dict[str, str], live: dict[str, str], ended: set[str]) -> None:
        obj = f"warehouse:{record['id']}"
        incarnation = require_incarnation(record, obj)
        project_incarnation = require_project_incarnation(record, obj)
        project = str(record["project"])
        if made_under(record, live.get(project), obj=obj):
            if self._client is None or record.get(BORN) == incarnation:
                return
            key = warehouses.record_key(str(record["id"]))
            if await fga.is_born(self._client, obj, incarnation):
                await run_in_threadpool(partial(_stamp_born, self._root, self._so, key, incarnation))
                return
            # Under a fence of the project incarnation the record names, so a project deleted since the listing above
            # leaves the record unborn, an orphan the next run ends (`GrantsChildBirthUnfenced`).
            async with fga.fenced(self._client, f"project:{project}", actor=ACTOR, origin="fence", incarnation=project_incarnation) as fence:
                if fence is not None:
                    with suppress(fga.IncarnationEnded):
                        await self._bear(obj, incarnation, key, fences=[fence])
            return
        report_orphan(obj, project=project, project_incarnation=project_incarnation)
        self.done.orphans.append(obj)
        # With authorization off no birth tells whether a door made anything in it, and once born it may hold grants and
        # data a door made: either way it is reported and kept, with its bucket's claim, until the estate admin purges it.
        if self._client is None or await fga.is_born(self._client, obj, incarnation):
            return
        if not self._may_end or not self._past_bound(record.get("created_at")):
            return
        stamped = await run_in_threadpool(
            partial(warehouses.end_warehouse_record, self._root, self._so, str(record["id"]), incarnation=incarnation, reason=ENDED_UNBORN)
        )
        if stamped is None:
            return
        log.warning(
            "warehouse_of_a_dead_tenant_ended",
            extra={"warehouse": record["id"], "project": project, "project_incarnation": project_incarnation, "bucket": record.get("bucket")},
        )
        ended.add(str(record["id"]))
        self.done.ended.append(obj)

    async def _claim(self, claim: dict[str, str], live: dict[str, str], standing: list[dict[str, str]]) -> None:
        """Release a claim a dead incarnation took that no record standing names, once it is older than the bound, where
        what stands in its bucket is only what a create writes (``warehouses.written_by_others``): a create refused after
        its claim, or one whose record the tick ended unborn. Never on a guess: anything else in the bucket keeps it."""
        bucket, project = claim["bucket"], claim["project"]
        project_incarnation = require_project_incarnation(claim, f"bucket:{bucket}")
        if made_under(claim, live.get(project), obj=f"bucket:{bucket}") or any(r.get("bucket") == bucket for r in standing):
            return
        if not self._may_release:
            return
        if (claimed := _written(claim.get(warehouses.CLAIMED_AT))) is None:
            log.error("claim_without_claimed_at", extra={"bucket": bucket})
            raise InternalError(f"the claim on bucket {bucket!r} names no time it was taken; every claim names one, so it was not written by this catalog")
        if claimed >= self._older_than:
            return
        if foreign := await run_in_threadpool(partial(warehouses.written_by_others, bucket, self._so, warehouse_id=claim["warehouse_id"])):
            log.error("bucket_claim_kept", extra={"bucket": bucket, "project": project, "project_incarnation": project_incarnation, "found": foreign})
            return
        if await run_in_threadpool(
            partial(
                warehouses.release_claim, self._root, self._so, bucket=bucket, project=project, project_incarnation=project_incarnation, reason=RELEASED_UNBORN
            )
        ):
            log.warning("bucket_claim_released", extra={"bucket": bucket, "project": project, "project_incarnation": project_incarnation})
            self.done.released.append(bucket)

    async def _policies(self, live: dict[str, str]) -> None:
        """Report each project policy an incarnation that no longer stands set (a set racing its project's delete). It is
        not ended: a write under the project's id here could only be unconditional, and would take a policy the tenant
        holding the id set meanwhile. It is harmless as it stands (ADR 0241 §9 P2): it governs only a bucket whose claim
        names the incarnation that set it (``maintenance_policies.governing_policy``), no door reads or merges onto it,
        and the next set under the id replaces it."""
        for record in await run_in_threadpool(maintenance_policies.list_policies, self._root, self._so):
            project = str(record.get("id"))
            if record.get("kind") != "project" or made_under(record, live.get(project), obj=f"policy:project:{project}"):
                continue
            report_orphan(f"policy:project:{project}", project=project, project_incarnation=str(record[PROJECT_INCARNATION]))
            self.done.orphans.append(f"policy:project:{project}")

    async def _sweep(self) -> None:
        assert self._client is not None
        self.done.swept = await fga.sweep_tickets(self._client, older_than=self._older_than)

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


def _stamp_born(control_root: str, storage_options: StorageOptions, key: str, incarnation: str) -> None:
    """Stamp the record at ``key`` born (:data:`BORN`) where it still names ``incarnation``; a record gone, or another
    incarnation's, is left as it is."""

    def stamp(record: dict[str, Any]) -> dict[str, Any]:
        if record.get(INCARNATION) != incarnation:
            raise RecordMissingError(key)
        return {**record, BORN: incarnation}

    with suppress(RecordMissingError):
        mutate_json(control_root, storage_options, key, stamp)


async def converge(
    control_root: str, storage_options: StorageOptions, client: OpenFgaClient | None, *, bound: timedelta, now: datetime | None = None
) -> IncarnationsConverged:
    """Bear every unborn incarnation of a live tenant, sweep the tickets older than ``bound``, and report, end and release
    what a dead tenant left; what was done.

    Idempotent and safe beside every door: a birth commits only while its marker is absent, so a create still running and
    this both bearing one incarnation make one birth, and the create then writes its seed under a fence; a ticket, an
    orphan or a claim is ended only past ``bound``, which a request's run does not reach, and judged by ``now``, the
    catalog's clock (:func:`fga.sweep_tickets` says what skew with the store's does). With authorization off nothing is
    born or swept and no record is ended, an orphan is only reported, and a claim no record names is released only on
    what its bucket holds.
    """
    return await _Convergence(control_root, storage_options, client, bound=bound, now=now or datetime.now(UTC)).run()
