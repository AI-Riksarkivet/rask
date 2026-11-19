"""The tick that converges the estate's tenants onto what git declares ([[XC-213]], ADR 0206).

The chart renders ``catalog.declared`` into a file the catalog mounts (``catalog.services.declaration``). Each tick
makes what the declaration names and is missing, corrects what drifted, releases what git stopped declaring, and
writes and revokes the tuples it asks for. Every change goes through the function the object's own door calls, so it
is audited and announced as a door's is, authored by the declaration. The catalog is the reconciler because it is
already the authorization store's only client: converging adds no workload and no identity.

It runs on the catalog's own timer (`catalog.api.timers`), once a window whatever the replica count; a tick that
overlaps another all the same repeats idempotent writes (conditional registry records, a grant record created once,
tuple writes that ignore duplicates).

One object's failure is logged and counted and leaves every other object to this tick; the next tick tries again.
A declaration that cannot be read converges nothing and releases nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, InvalidInputError
from openfga_sdk import OpenFgaClient
from opentelemetry import metrics
from pydantic import BaseModel, Field

from catalog.api import announcing, fga_deps
from catalog.api.announcing import Announcer, get_announcer
from catalog.api.declared import DECLARATION
from catalog.api.dependencies import ObjectUuids, Resolver, get_fga_client, get_vendor
from catalog.api.ticks import Tick
from catalog.api.v1.endpoints import projects as project_doors
from catalog.api.v1.endpoints import warehouses as warehouse_doors
from catalog.core.config import Settings
from catalog.core.vending import CredentialVendor
from catalog.schemas import CreateWarehouseRequest, DeleteProfile
from catalog.services import declaration, project_admins, tenancy, tenancy_converge, warehouses
from catalog.services import projects as project_registry
from catalog.services.control_records import DELETE_RUN, GIT, MANAGED_BY, UUID, DeleteMode, ProtectionChange
from catalog.services.declaration import Declaration, DeclaredProject, DeclaredWarehouse, GrantTuple
from lakehouse import protection
from lakehouse.control_emit import ControlEmitter
from lakehouse.governed import fga
from lakehouse.governed.tenancy import PROJECT, Kind, kind_of


log = logging.getLogger(__name__)

_failed = metrics.get_meter(__name__).create_counter(
    "catalog.declaration_converge.failed",
    unit="1",
    description="Objects of the mounted declaration a converge tick could not make or correct; the next tick tries again.",
)
_PROTECTED_BY = ProtectionChange(protected=True, by=DECLARATION.actor)
_UNPROTECTED_BY = ProtectionChange(protected=False, by=DECLARATION.actor)


class ConvergeTick(BaseModel):
    """What one tick did."""

    skipped: bool = False
    #: Each project and warehouse made or corrected, as ``project:<id>`` / ``warehouse:<id>``.
    converged: list[str] = Field(default_factory=list)
    #: Each one git stopped declaring, now its own doors' to change.
    released: list[str] = Field(default_factory=list)
    grants_written: int = 0
    grants_revoked: int = 0
    #: What could not be converged this tick, each as ``<object>: <why>``.
    failed: list[str] = Field(default_factory=list)


class _Converger:
    """One tick's work: the collaborators a door would be handed, and the tick it fills in."""

    def __init__(
        self, request: Request, settings: Settings, *, client: OpenFgaClient | None, control: ControlEmitter, announcer: Announcer, vendor: CredentialVendor
    ) -> None:
        self._request = request
        self._settings = settings
        self._client = fga_deps.wired(settings, client)
        self._control = control
        self._announcer = announcer
        self._vendor = vendor
        self._root = settings.registry_root
        self._so = settings.storage_options()
        self.tick = ConvergeTick()

    async def run(self, declared: Declaration) -> ConvergeTick:
        """Converge the estate onto ``declared``: projects, then the warehouses in them, then what git released, then grants."""
        for project in declared.projects:
            await self._attempt(f"project:{project.id}", partial(self._project, project))
        for warehouse in declared.warehouses:
            await self._attempt(f"warehouse:{warehouse.id}", partial(self._warehouse, warehouse))
        await self._attempt("release", partial(self._release, declared))
        await self._attempt("grants", partial(self._grants, declared))
        return self.tick

    async def _attempt(self, what: str, step: Callable[[], Awaitable[None]]) -> bool:
        """Run ``step``; whether it ran to its end. A step that did not is named among the tick's failures, with why."""
        try:
            await step()
        except Exception as exc:  # noqa: BLE001 — one object's failure leaves every other object to this tick
            log.warning("declaration_converge_failed", extra={"object": what}, exc_info=True)
            _failed.add(1, {"lance.catalog.object": what})
            self.tick.failed.append(f"{what}: {exc}")
            return False
        return True

    async def _project(self, declared: DeclaredProject) -> None:
        record = await run_in_threadpool(project_registry.get_project, self._root, self._so, declared.id)
        changed = False
        if record is None:
            record = await project_doors.establish_project(
                self._settings, self._client, self._announcer, project_id=declared.id, admin=None, author=DECLARATION
            )
            changed = True
        if protection.is_protected(record) != declared.protected:
            change = _PROTECTED_BY if declared.protected else _UNPROTECTED_BY
            await project_doors.protect_project(self._settings, self._control, declared.id, uuid=record[UUID], change=change, actor=DECLARATION.actor)
            changed = True
        if record.get(MANAGED_BY) != GIT:
            await run_in_threadpool(partial(project_registry.set_project_manager, self._root, self._so, declared.id, uuid=record[UUID], manager=GIT))
            changed = True
        if changed:
            self.tick.converged.append(f"project:{declared.id}")

    async def _warehouse(self, declared: DeclaredWarehouse) -> None:
        record = await run_in_threadpool(warehouses.get_warehouse, self._root, self._so, declared.id)
        if record is not None and record.get("project") != declared.project:
            # Refused before its status, protection or mark is touched: converging it would quarantine or lock one
            # project's warehouse on the word of a declaration that places it in another.
            raise InvalidInputError(
                f"warehouse {declared.id!r} stands in project {record.get('project')!r} and git declares it in {declared.project!r}: "
                "a warehouse does not move between projects"
            )
        changed = False
        # The mark is written only once an establish has run to its end, so a warehouse whose establish failed after
        # its record (its catalog row, its grants) is established again by the next tick and is not locked meanwhile.
        if record is None or record.get(MANAGED_BY) != GIT or _spec_drifted(record, declared):
            body = CreateWarehouseRequest(
                id=declared.id,
                project=declared.project,
                bucket=declared.bucket,
                delete_profile=declared.delete_profile or DeleteProfile(mode=DeleteMode.ESTATE),
            )
            ids = warehouse_doors.validated_ids(self._settings, body)
            project = await fga_deps.require_project_exists(self._settings, declared.project)
            record = await warehouse_doors.establish_warehouse(
                self._request,
                self._settings,
                body,
                ids,
                client=self._client,
                control=self._control,
                vendor=self._vendor,
                author=DECLARATION,
                owner=None,
                project=project,
            )
            if record.get(MANAGED_BY) != GIT:
                await run_in_threadpool(partial(warehouses.set_warehouse_manager, self._root, self._so, declared.id, uuid=record[UUID], manager=GIT))
            changed = True
        status: warehouse_doors.WarehouseStatus = "active" if declared.active else "deactivated"
        if (record.get("status") or "active") != status:
            await warehouse_doors.change_warehouse_status(self._settings, self._control, declared.id, uuid=record[UUID], status=status, actor=DECLARATION.actor)
            changed = True
        if protection.is_protected(record) != declared.protected:
            change = _PROTECTED_BY if declared.protected else _UNPROTECTED_BY
            await warehouse_doors.protect_warehouse(self._settings, self._control, declared.id, uuid=record[UUID], change=change, actor=DECLARATION.actor)
            changed = True
        if changed:
            self.tick.converged.append(f"warehouse:{declared.id}")

    async def _release(self, declared: Declaration) -> None:
        """Hand back to its own doors each project and warehouse git stopped declaring; its data and grants stay."""
        projects = {project.id for project in declared.projects}
        for record in await run_in_threadpool(project_registry.list_projects, self._root, self._so):
            if record.get(MANAGED_BY) == GIT and record["id"] not in projects:
                await run_in_threadpool(partial(project_registry.set_project_manager, self._root, self._so, record["id"], uuid=record[UUID], manager=None))
                self.tick.released.append(f"project:{record['id']}")
        declared_warehouses = {warehouse.id for warehouse in declared.warehouses}
        for record in await run_in_threadpool(warehouses.list_warehouses, self._root, self._so):
            if record.get(MANAGED_BY) == GIT and record["id"] not in declared_warehouses:
                await run_in_threadpool(partial(warehouses.set_warehouse_manager, self._root, self._so, record["id"], uuid=record[UUID], manager=None))
                self.tick.released.append(f"warehouse:{record['id']}")

    async def _grants(self, declared: Declaration) -> None:
        """Settle every intent a tick left, write each declared tuple the store lacks, and revoke each one written from a
        declaration git no longer makes.

        A declared tuple the store already holds is left as it is and is not recorded: somebody made it at a door, and
        it is theirs when git stops declaring it. A tuple the converge writes is recorded first as an intent naming an
        evidence the same Write leaves (ADR 0241): a tick that dies anywhere leaves the intent, and the next settles it
        by whether the evidence stands, so a door's tuple is never recorded as git's and git's is never left unrecorded.

        The admins of a project git stopped declaring are not revoked: a released project is its doors' as it stands,
        its people included, so its admins are handed over by forgetting that the declaration wrote them.

        Nothing is revoked on an object whose grant failed this tick, so a principal git replaced keeps its rungs until
        its successor holds them: moving the estate's administrator never leaves the estate with none between ticks.
        """
        if self._client is None:
            return
        for pending in [record for record in await run_in_threadpool(declaration.ledger, self._root, self._so) if record.evidence is not None]:
            await self._attempt(f"settle:{pending.object}#{pending.relation}", partial(self._settle, self._client, pending))
        wanted = self._wanted(declared)
        recorded = {record.grant: record for record in await run_in_threadpool(declaration.ledger, self._root, self._so) if record.evidence is None}
        ungranted: set[str] = set()
        for grant in sorted(wanted, key=_order):
            if (on := await self._on_creation(grant)) is not None and await fga.holds(self._client, on.client_tuple):
                continue
            if not await self._attempt(f"grant:{grant.object}#{grant.relation}", partial(self._grant, self._client, grant, recorded.get(grant))):
                ungranted.add(grant.object)
        released = {f"project:{record['id']}" for record in await run_in_threadpool(project_registry.list_projects, self._root, self._so)} - {
            f"project:{project.id}" for project in declared.projects
        }
        for grant in sorted(set(recorded) - wanted, key=_order):
            if grant.object in ungranted:
                continue
            if grant.relation == project_admins.ADMIN and grant.object in released:
                await run_in_threadpool(declaration.forget_record, self._root, self._so, recorded[grant])
                continue
            await self._attempt(f"revoke:{grant.object}#{grant.relation}", partial(self._revoke, self._client, recorded[grant]))
        for obj in self._estate_objects:
            await self._attempt(f"own:{obj}", partial(self._revoke_undeclared_own_roles, self._client, obj, wanted))

    async def _settle(self, client: OpenFgaClient, record: declaration.GrantRecord) -> None:
        """Settle an intent a tick left: the declaration's where its evidence stands, forgotten where it does not."""
        proof = fga.ClientTuple(user=str(record.evidence), relation=fga.REVISION, object=_written_on(record))
        if await fga.holds(client, proof):
            await run_in_threadpool(declaration.settle_grant, self._root, self._so, record)
            await fga.delete_tuples(client, [proof], actor=DECLARATION.subject, origin="declaration")  # noqa: TID251
        else:
            await run_in_threadpool(declaration.forget_record, self._root, self._so, record)

    @property
    def _estate_objects(self) -> tuple[str, str]:
        """The estate root and the estate's own warehouse, which stand with no registry record."""
        return self._settings.fga_root_object, self._settings.fga_default_warehouse_object

    def _wanted(self, declared: Declaration) -> set[GrantTuple]:
        """What the declaration asks for, and the maintainer rung on the estate's own warehouse.

        Every registered warehouse's maintainers are written with it and converged at boot (``fga_deps.cascade_tuples``);
        the estate's own warehouse has no record to be written with, so its maintainers converge here, from the same setting.
        """
        root, estate_warehouse = self._estate_objects
        maintainers = {GrantTuple(user=t.user, relation=t.relation, object=t.object) for t in fga_deps.maintainer_tuples(self._settings, estate_warehouse)}
        return declared.tuples(root=root, estate_warehouse=estate_warehouse) | maintainers

    async def _revoke_undeclared_own_roles(self, client: OpenFgaClient, obj: str, wanted: set[GrantTuple]) -> None:
        """Take back from the release's own accounts each machine role on ``obj`` git does not declare.

        The release's accounts are its own (``LANCE_OWN_ACCOUNTS``): no door grants one a machine role on the estate, so
        a role git stopped declaring is revoked whether or not this ledger wrote it. A role naming any other principal
        was granted at a door and stays ([[LH-563]]).
        """
        own = fga_deps.own_users(self._settings)
        if not own:
            return
        declared = [grant.client_tuple for grant in wanted if grant.object == obj]
        for relation in sorted(fga.machine_relations(obj.partition(":")[0])):
            revoked = await fga.revoke_undeclared(  # noqa: TID251 — the declaration's revoking site
                client, declared, obj=obj, relation=relation, own=own, actor=DECLARATION.subject, origin="declaration"
            )
            for gone in revoked:
                await run_in_threadpool(declaration.forget_grant, self._root, self._so, GrantTuple(user=gone.user, relation=gone.relation, object=gone.object))
            self.tick.grants_revoked += len(revoked)

    async def _grant(self, client: OpenFgaClient, grant: GrantTuple, settled: declaration.GrantRecord | None) -> None:
        """Write ``grant`` on the creation its object's id names now (ADR 0245): git declares the id, so a project or a
        warehouse made again under it is the new creation's, and it is granted there in the same tick.

        The ledger's record of a declared grant is about ONE creation (review T1). ``settled``, the record this tick read,
        names the creation a grant was written on; the grant is not held on the live creation, so a record of another
        creation (one that ended) is forgotten, and one of this creation (its tuple revoked at a door since) is too, and
        the grant is declared afresh. Nothing is skipped silently: a grant this tick neither holds nor writes raises,
        and the tick names it among its failures with why.

        Raises:
            LookupError: No live creation holds the id.
            ConcurrentModificationError: Another tick's intent on the grant stands unsettled.
        """
        if (on := await self._on_creation(grant)) is None:
            raise LookupError(f"{grant.object} has no live registry record, so no grant on it is written")
        # Read again right before the intent: a tuple somebody made at a door since the tick's first read is theirs.
        if await fga.holds(client, on.client_tuple):
            return
        if settled is not None:
            await run_in_threadpool(declaration.forget_record, self._root, self._so, settled)
        proof = fga.evidence(on.object)
        if not await run_in_threadpool(partial(declaration.record_intent, self._root, self._so, grant, evidence=str(proof.user), uuid=on.uuid)):
            raise ConcurrentModificationError(
                f"an intent to write {grant.object}#{grant.relation} another tick recorded stands unsettled; the next tick settles it"
            )
        told = announcing.grant_announcement(
            on.client_tuple, action="grant_added", actor=DECLARATION.actor, object_name=on.name, object_uuid=on.uuid, extra={"reason": "declared"}
        )
        async with self._announcer.announcing(door="declaration_converge", target=grant.object, announcements=[told]) as filing:
            record = declaration.GrantRecord(**grant.model_dump(), evidence=str(proof.user), uuid=on.uuid)
            if not await fga.write_evidenced(client, on.client_tuple, proof, actor=DECLARATION.subject, origin="declaration"):
                # A door made the tuple between the read above and this Write: it is the door's and it is held, and the
                # intent goes.
                await run_in_threadpool(declaration.forget_record, self._root, self._so, record)
                return
            await self._settle(client, record)
            await filing.send(told)
        self.tick.grants_written += 1

    async def _revoke(self, client: OpenFgaClient, record: declaration.GrantRecord) -> None:
        """Revoke a tuple git stopped declaring, on the creation the ledger says it was written on: a project or a
        warehouse deleted since took it with everything else on it, and one made again under the id holds nothing this
        ledger wrote (ADR 0245)."""
        grant = record.grant
        on = await self._on_creation(grant)
        if on is None or on.uuid != record.uuid:
            await run_in_threadpool(declaration.forget_record, self._root, self._so, record)
            return
        told = announcing.grant_announcement(
            on.client_tuple, action="grant_revoked", actor=DECLARATION.actor, object_name=on.name, object_uuid=on.uuid, extra={"reason": "undeclared"}
        )
        async with self._announcer.announcing(door="declaration_converge", target=grant.object, announcements=[told]) as filing:
            if grant.relation == project_admins.ADMIN and grant.object.startswith("project:"):
                # Through the members door's own rule, which keeps a project administered.
                await project_admins.withdraw(client, [on.client_tuple], actor=DECLARATION.subject, origin="declaration")
            else:
                await fga.delete_tuples(client, [on.client_tuple], actor=DECLARATION.subject, origin="declaration")  # noqa: TID251
            await filing.send(told)
        await run_in_threadpool(declaration.forget_record, self._root, self._so, record)
        self.tick.grants_revoked += 1

    async def _on_creation(self, grant: GrantTuple) -> _OnCreation | None:
        """``grant`` as it is written: on the estate's own objects as declared, which no record names and nothing deletes;
        on a project or a warehouse, on the live creation its id names (``tenancy.resolved``, ADR 0245). ``None`` where no
        live creation holds the id."""
        if grant.object in self._estate_objects:
            return _OnCreation(object=grant.object, name=grant.object, uuid=None, user=grant.user, relation=grant.relation)
        name, _, object_id = grant.object.partition(":")
        if (kind := kind_of(name)) is None or (tenant := await tenancy.resolved(self._settings, kind, object_id)) is None:
            return None
        return _OnCreation(object=tenant.obj, name=object_id, uuid=tenant.uuid, user=grant.user, relation=grant.relation)


class _OnCreation(BaseModel):
    """A declared grant as it is written: the object it stands on (a creation's, or an estate object), the id it was
    declared on, and the creation's uuid, ``None`` for an estate object."""

    object: str
    name: str
    uuid: str | None
    user: str
    relation: str

    @property
    def client_tuple(self) -> fga.ClientTuple:
        return fga.ClientTuple(user=self.user, relation=self.relation, object=self.object)


def _written_on(record: declaration.GrantRecord) -> str:
    """The object the ledger's ``record`` was written on: its creation's, or the estate object it names."""
    if record.uuid is None:
        return record.object
    if (kind := kind_of(record.object.partition(":")[0])) is None:
        return record.object
    return tenancy.object_of_record(kind, {UUID: record.uuid})


def _order(grant: GrantTuple) -> tuple[str, str, str]:
    return grant.object, grant.relation, grant.user


def _spec_drifted(record: dict[str, str], declared: DeclaredWarehouse) -> bool:
    """Whether a standing warehouse's bucket or delete profile differs from what git declares of it.

    A record naming no delete mode and one naming the estate's are the same profile: a new record is written with the
    mode, and a merge into a standing one removes it.
    """
    profile = declared.delete_profile
    mode = None if profile is None or profile.mode is DeleteMode.ESTATE else str(profile.mode)
    grace = None if profile is None or profile.grace_days is None else str(profile.grace_days)
    held_mode = record.get(warehouses.DELETE_MODE)
    if held_mode == DeleteMode.ESTATE.value:
        held_mode = None
    return (record.get("bucket"), held_mode, record.get(warehouses.TRASH_GRACE_DAYS)) != (declared.bucket or declared.id, mode, grace)


def finisher(ctx: Tick) -> tenancy_converge.Finisher:
    """What finishes a delete past its point of no return whose run died: the delete door's own steps, run by the tick as
    no caller (ADR 0245, second and third reviews). Its authorization was asked of the delete that passed the stamp, and a
    delete resumed past one judges no check. The tick acts on its listing only through the door's own fresh read of the
    record, which must still carry the run the listing found past its stamp (``finishing``), and takes the mark over
    under that read's ETag: a record finished, taken over, or another creation's since the listing is left as it is,
    so the tick never begins a delete. The steps left are run as the stamp records the delete was asked
    (``tenancy.asked_of``): bytes are purged only where the delete that passed the stamp was asked to, under the claim
    its purge took (ADR 0241 §10)."""
    request, settings = ctx.request, ctx.settings
    client = get_fga_client(request)
    announcer = get_announcer(request, settings, client, Resolver(request, settings), ctx.control, ObjectUuids(request, settings))

    async def finish(kind: Kind, record: dict[str, Any]) -> None:
        listed_run = str(record[DELETE_RUN])
        if kind == PROJECT:
            await project_doors.remove_project(str(record["id"]), record, settings, None, client, announcer, force=False, finishing=listed_run)
            return
        await warehouse_doors.remove_warehouse(
            str(record["id"]),
            record,
            request,
            settings,
            None,
            client,
            announcer,
            ObjectUuids(request, settings),
            cascade=False,
            purge_bucket=False,
            force=False,
            finishing=listed_run,
        )

    return finish


async def tick(ctx: Tick) -> ConvergeTick:
    """Finish or end every creating project and warehouse record, settle every mark a dead delete left and sweep what a
    dead creation left (``tenancy_converge.converge``, ADR 0245), then the estate's tenants onto the mounted declaration;
    a deployment that mounts none converges only the tenancy. What the tenancy's convergence could not converge is
    reported among the tick's failures."""
    request, settings = ctx.request, ctx.settings
    tenanted = await tenancy_converge.converge(
        settings,
        fga_deps.wired(settings, get_fga_client(request)),
        bound=tenancy.create_bound(settings.request_deadline_seconds),
        connections=request.app.state.connections,
        finish=finisher(ctx),
    )
    if not settings.declaration_file:
        return ConvergeTick(skipped=True, failed=tenanted.failed)
    try:
        declared = await run_in_threadpool(declaration.load, settings.declaration_file)
    except declaration.DeclarationUnreadableError:
        log.error("declaration_unreadable", extra={"path": settings.declaration_file}, exc_info=True)
        _failed.add(1, {"lance.catalog.object": "declaration"})
        return ConvergeTick(failed=["declaration", *tenanted.failed])
    client = get_fga_client(request)
    # A door's announcer, for a change no caller made: the tick's scope carries no originator and no bearer.
    announcer = get_announcer(request, settings, client, Resolver(request, settings), ctx.control, ObjectUuids(request, settings))
    converged = await _Converger(request, settings, client=client, control=ctx.control, announcer=announcer, vendor=get_vendor(request)).run(declared)
    converged.failed += tenanted.failed
    if converged.converged or converged.released or converged.grants_written or converged.grants_revoked or converged.failed:
        log.info("declaration_converged", extra=converged.model_dump())
    return converged
