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

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidInputError
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
from catalog.services import declaration, incarnations, project_admins, warehouses
from catalog.services import projects as project_registry
from catalog.services.control_records import GIT, MANAGED_BY, DeleteMode, ProtectionChange
from catalog.services.declaration import Declaration, DeclaredProject, DeclaredWarehouse, GrantTuple
from lakehouse import protection
from lakehouse.control_emit import ControlEmitter
from lakehouse.governed import fga


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
    #: What could not be converged this tick.
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
        """Run ``step``; whether it ran to its end."""
        try:
            await step()
        except Exception:  # noqa: BLE001 — one object's failure leaves every other object to this tick
            log.warning("declaration_converge_failed", extra={"object": what}, exc_info=True)
            _failed.add(1, {"lance.catalog.object": what})
            self.tick.failed.append(what)
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
            await project_doors.protect_project(self._settings, self._control, declared.id, change=change, actor=DECLARATION.actor)
            changed = True
        if record.get(MANAGED_BY) != GIT:
            await run_in_threadpool(project_registry.set_project_manager, self._root, self._so, declared.id, manager=GIT)
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
            await fga_deps.require_project_exists(self._settings, declared.project)
            record = await warehouse_doors.establish_warehouse(
                self._request, self._settings, body, ids, client=self._client, control=self._control, vendor=self._vendor, author=DECLARATION, owner=None
            )
            if record.get(MANAGED_BY) != GIT:
                await run_in_threadpool(warehouses.set_warehouse_manager, self._root, self._so, declared.id, manager=GIT)
            changed = True
        status: warehouse_doors.WarehouseStatus = "active" if declared.active else "deactivated"
        if (record.get("status") or "active") != status:
            await warehouse_doors.change_warehouse_status(self._settings, self._control, declared.id, status=status, actor=DECLARATION.actor)
            changed = True
        if protection.is_protected(record) != declared.protected:
            change = _PROTECTED_BY if declared.protected else _UNPROTECTED_BY
            await warehouse_doors.protect_warehouse(
                self._settings, self._control, declared.id, project=declared.project, change=change, actor=DECLARATION.actor
            )
            changed = True
        if changed:
            self.tick.converged.append(f"warehouse:{declared.id}")

    async def _release(self, declared: Declaration) -> None:
        """Hand back to its own doors each project and warehouse git stopped declaring; its data and grants stay."""
        projects = {project.id for project in declared.projects}
        for record in await run_in_threadpool(project_registry.list_projects, self._root, self._so):
            if record.get(MANAGED_BY) == GIT and record["id"] not in projects:
                await run_in_threadpool(project_registry.set_project_manager, self._root, self._so, record["id"], manager=None)
                self.tick.released.append(f"project:{record['id']}")
        declared_warehouses = {warehouse.id for warehouse in declared.warehouses}
        for record in await run_in_threadpool(warehouses.list_warehouses, self._root, self._so):
            if record.get(MANAGED_BY) == GIT and record["id"] not in declared_warehouses:
                await run_in_threadpool(warehouses.set_warehouse_manager, self._root, self._so, record["id"], manager=None)
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
        held = {obj: await _held(self._client, obj) for obj in sorted({grant.object for grant in wanted | set(recorded)})}
        ungranted: set[str] = set()
        for grant in sorted(wanted, key=_order):
            if (grant.user, grant.relation) in held[grant.object]:
                continue
            if not await self._attempt(f"grant:{grant.object}#{grant.relation}", partial(self._grant, self._client, grant)):
                ungranted.add(grant.object)
        released = {f"project:{record['id']}" for record in await run_in_threadpool(project_registry.list_projects, self._root, self._so)} - {
            f"project:{project.id}" for project in declared.projects
        }
        for grant in sorted(set(recorded) - wanted, key=_order):
            if grant.object in ungranted:
                continue
            if grant.relation == project_admins.ADMIN and grant.object in released:
                await run_in_threadpool(declaration.forget_grant, self._root, self._so, grant)
                continue
            await self._attempt(f"revoke:{grant.object}#{grant.relation}", partial(self._revoke, self._client, recorded[grant]))
        for obj in self._estate_objects:
            await self._attempt(f"own:{obj}", partial(self._revoke_undeclared_own_roles, self._client, obj, wanted))

    async def _settle(self, client: OpenFgaClient, record: declaration.GrantRecord) -> None:
        """Settle an intent a tick left: the declaration's where its evidence stands, forgotten where it does not."""
        proof = fga.ClientTuple(user=str(record.evidence), relation=fga.REVISION, object=record.object)
        if await fga.evidence_stands(client, proof):
            await run_in_threadpool(declaration.settle_grant, self._root, self._so, record)
            await fga.delete_tuples(client, [proof], actor=DECLARATION.subject, origin="declaration")  # noqa: TID251
        else:
            await run_in_threadpool(declaration.forget_grant, self._root, self._so, record.grant)

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

    async def _grant(self, client: OpenFgaClient, grant: GrantTuple) -> None:
        if (stands := await self._stands(grant.object)) is None:
            raise LookupError(f"{grant.object} has no registry record, so no grant on it is written")
        fences = await self._fences(client, grant.object, stands)
        try:
            # Read again right before the intent: a tuple somebody made at a door since the tick's first read is theirs.
            if (grant.user, grant.relation) in await _held(client, grant.object):
                return
            proof = fga.evidence(grant.object)
            if not await run_in_threadpool(
                partial(declaration.record_intent, self._root, self._so, grant, evidence=str(proof.user), incarnation=stands.incarnation)
            ):
                return
            told = announcing.grant_announcement(grant.client_tuple, action="grant_added", actor=DECLARATION.actor, extra={"reason": "declared"})
            async with self._announcer.announcing(door="declaration_converge", target=grant.object, announcements=[told]) as filing:
                record = declaration.GrantRecord(**grant.model_dump(), evidence=str(proof.user), incarnation=stands.incarnation)
                try:
                    written = await fga.write_evidenced(client, fences, grant.client_tuple, proof, actor=DECLARATION.subject, origin="declaration")
                except fga.IncarnationEnded:
                    await run_in_threadpool(declaration.forget_grant, self._root, self._so, grant)
                    raise
                if not written:
                    # A door made the tuple between the read above and this Write: it is the door's, and the intent goes.
                    await run_in_threadpool(declaration.forget_grant, self._root, self._so, grant)
                    return
                await self._settle(client, record)
                await filing.send(told)
            self.tick.grants_written += 1
        finally:
            await fga.close_fences(client, fences, actor=DECLARATION.subject, origin="fence")

    async def _revoke(self, client: OpenFgaClient, record: declaration.GrantRecord) -> None:
        grant = record.grant
        if (stands := await self._stands(grant.object)) is None:
            # Its object's delete revoked the tuple with everything else on it.
            await run_in_threadpool(declaration.forget_grant, self._root, self._so, grant)
            return
        fences = await self._fences(client, grant.object, stands)
        try:
            if stands.incarnation != record.incarnation:
                # Written on an incarnation since deleted, whose delete revoked it: what stands now is another's.
                await run_in_threadpool(declaration.forget_grant, self._root, self._so, grant)
                return
            told = announcing.grant_announcement(grant.client_tuple, action="grant_revoked", actor=DECLARATION.actor, extra={"reason": "undeclared"})
            async with self._announcer.announcing(door="declaration_converge", target=grant.object, announcements=[told]) as filing:
                if grant.relation == project_admins.ADMIN and grant.object.startswith("project:"):
                    # Through the members door's own rule, which keeps a project administered.
                    await project_admins.withdraw(client, [grant.client_tuple], actor=DECLARATION.subject, origin="declaration", fences=fences)
                elif fences:
                    await fga.commit_fenced(client, fences, deletes=[grant.client_tuple], actor=DECLARATION.subject, origin="declaration")
                else:
                    await fga.delete_tuples(client, [grant.client_tuple], actor=DECLARATION.subject, origin="declaration")  # noqa: TID251
                await filing.send(told)
            await run_in_threadpool(declaration.forget_grant, self._root, self._so, grant)
            self.tick.grants_revoked += 1
        finally:
            await fga.close_fences(client, fences, actor=DECLARATION.subject, origin="fence")

    @staticmethod
    async def _fences(client: OpenFgaClient, obj: str, stands: _Standing) -> list[fga.Fence]:
        """A fence on ``obj`` of the incarnation its record names (ADR 0241); none on the estate's own objects.

        Raises:
            fga.IncarnationEnded: No incarnation stands, or another than the record's: its delete has begun.
        """
        if stands.incarnation is None:
            return []
        fence = await fga.open_fence(client, obj, actor=DECLARATION.subject, origin="fence")
        if fence is None or fence.incarnation != stands.incarnation:
            if fence is not None:
                await fga.close_fences(client, [fence], actor=DECLARATION.subject, origin="fence")
            raise fga.IncarnationEnded(f"{obj} is not the incarnation its record names: it is being deleted, or still being established")
        return [fence]

    async def _stands(self, obj: str) -> _Standing | None:
        """What a grant on ``obj`` is written under: ``None`` where no record names it, no incarnation for the estate's own
        objects, which no record names and nothing deletes, and the record's for a project or warehouse.

        Raises:
            SettlingError: The record names no incarnation yet: the catalog converges it (``incarnations.converge``).
        """
        if obj in self._estate_objects:
            return _Standing(incarnation=None)
        kind, _, object_id = obj.partition(":")
        if kind == "project":
            record = await run_in_threadpool(project_registry.get_project, self._root, self._so, object_id)
        else:
            record = await run_in_threadpool(warehouses.get_warehouse, self._root, self._so, object_id)
        if record is None:
            return None
        return _Standing(incarnation=incarnations.require_incarnation(record, obj))


class _Standing(BaseModel):
    """An object a declared grant is written on: the incarnation it is written under, ``None`` for an estate object."""

    incarnation: str | None


async def _held(client: OpenFgaClient, obj: str) -> set[tuple[str, str]]:
    """Each ``(user, relation)`` the store holds on ``obj``."""
    return {(held.user, held.relation) for held in await fga.read_object_tuples(client, obj)}


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


async def tick(ctx: Tick) -> ConvergeTick:
    """Converge every project's and warehouse's incarnation (``incarnations.converge``, ADR 0241), then the estate's
    tenants onto the mounted declaration; a deployment that mounts none converges only the incarnations."""
    request, settings = ctx.request, ctx.settings
    await incarnations.converge(settings.registry_root, settings.storage_options(), fga_deps.wired(settings, get_fga_client(request)))
    if not settings.declaration_file:
        return ConvergeTick(skipped=True)
    try:
        declared = await run_in_threadpool(declaration.load, settings.declaration_file)
    except declaration.DeclarationUnreadableError:
        log.error("declaration_unreadable", extra={"path": settings.declaration_file}, exc_info=True)
        _failed.add(1, {"lance.catalog.object": "declaration"})
        return ConvergeTick(failed=["declaration"])
    client = get_fga_client(request)
    # A door's announcer, for a change no caller made: the tick's scope carries no originator and no bearer.
    announcer = get_announcer(request, settings, client, Resolver(request, settings), ctx.control, ObjectUuids(request, settings))
    converged = await _Converger(request, settings, client=client, control=ctx.control, announcer=announcer, vendor=get_vendor(request)).run(declared)
    if converged.converged or converged.released or converged.grants_written or converged.grants_revoked or converged.failed:
        log.info("declaration_converged", extra=converged.model_dump())
    return converged
