"""The converge tick's half of tenancy (ADR 0245): every creating record is finished or ended, what a write racing a
delete left on an ended creation is swept, and what a dead creation left in the registry and in a bucket is reported,
ended or released.

It runs at boot and on each converge tick (``main.converge_at_boot``, ``declaration_converge.tick``), so nothing is
hand-run:

- A project record still ``creating`` (its create died between its record and its flip) is finished from what it names:
  its seed admin on its uuid, then ``live``. A warehouse record still ``creating`` whose create no longer runs (older than
  :func:`tenancy.create_bound`) is finished the same way where its project's creation it names is live and its bucket's
  claim names it, its catalog row ensured first; where its project's creation is not live, it is ended: its object
  revoked, its record ended, and its bucket's claim released on the terms the bucket half sets.
- An ended creation is swept: what stands on its object and names it is revoked (``fga.revoke_object_tuples``), and it is
  forgotten once a sweep past the bound found nothing, since no door's write can land on it after that.
- A role or a project policy naming a project creation that is not live is reported; a role is ended.
- A bucket claim of a dead creation that no record names is released where the bucket holds only what a create writes
  (``warehouses.written_by_others``), once the tick, holding the claim, has removed exactly that and found the bucket
  empty; anything else keeps the claim, reported as ``bucket:<name>``, which the estate owner's purge reaches by its
  bucket (ADR 0241 §10 Q2). A claim a warehouse delete's purge stamped whose record is gone is released.

Every listing is tolerant: one record that cannot be read is named among the failures and stops nothing but what it
could decide (ADR 0241 §10 Q3).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from lance_namespace import InternalError
from opentelemetry import metrics
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from catalog.api import fga_deps
from catalog.services import control_records, projects, roles, warehouses
from catalog.services.tenancy import ACTOR, object_of_record
from lakehouse import maintenance_policies
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.tenancy import CREATING, LIVE, PROJECT, PROJECT_UUID, STATE, UUID, WAREHOUSE, made_under


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient

    from catalog.core.config import Settings
    from catalog.core.namespace import NamespaceConnections


log = logging.getLogger(__name__)

_orphans = metrics.get_meter("lance.catalog").create_counter(
    "catalog.tenancy.orphans",
    unit="1",
    description="Records a dead creation left (a role or project policy naming a project creation that is not live, or a bucket claim of one no record names), each time one is found.",
)

#: Why the converge tick released a bucket's claim.
RELEASED_UNBORN: Final = "claimed_by_a_dead_creation"


class TenancyConverged(BaseModel):
    """What one run did, each object as ``project:<id>``, ``warehouse:<id>``, ``role:<uuid>`` or ``bucket:<name>``."""

    #: Creating records finished, flipped live.
    finished: list[str] = Field(default_factory=list)
    #: Creating warehouse records of a dead project creation ended, and orphan roles ended.
    ended: list[str] = Field(default_factory=list)
    #: Tuples revoked from ended creations, by the object they stood on.
    swept: dict[str, int] = Field(default_factory=dict)
    #: Records a dead creation left, found and refused.
    orphans: list[str] = Field(default_factory=list)
    #: The buckets whose claim a dead creation took was released.
    released: list[str] = Field(default_factory=list)
    #: Objects that could not be converged, with why; the next run tries again.
    failed: list[str] = Field(default_factory=list)


def report_orphan(obj: str, *, project: str, project_uuid: str) -> None:
    """Report a record a dead creation left: ``obj`` names ``project`` at a creation that is not live."""
    _orphans.add(1, {"lance.catalog.object_type": obj.partition(":")[0]})
    log.error("record_of_a_dead_creation", extra={"object": obj, "project": project, "project_uuid": project_uuid})


def _written(stamp: str | datetime | None) -> datetime | None:
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(stamp).astimezone(UTC) if stamp else None
    except ValueError:
        return None


class _Convergence:
    """One run over the registry and OpenFGA: ONE listing of each registry, and OpenFGA requests only for what needs
    work (a creating record, an ended creation, an orphan), never one per record."""

    def __init__(self, settings: Settings, client: OpenFgaClient | None, *, bound: timedelta, now: datetime, connections: NamespaceConnections) -> None:
        self._settings, self._client, self._connections = settings, client, connections
        self._root, self._so = settings.registry_root, settings.storage_options()
        self._now = now
        self._older_than = now - bound
        self._may_end = False
        self._may_release = False
        self.done = TenancyConverged()

    async def run(self) -> TenancyConverged:
        listed = await self._listing("projects", partial(projects.read_projects, self._root, self._so))
        if listed is not None:
            records, unreadable = listed
            # A project record that cannot be read may name the creation a record was made under, so nothing is judged a
            # dead creation's while one cannot: orphans are reported and kept.
            self._may_end = not unreadable
            for record in records:
                if record.get(STATE) == CREATING:
                    await self._attempt(f"project:{record['id']}", partial(self._finish_project, record))
            finished = {f.removeprefix("project:") for f in self.done.finished}
            live = {str(r["id"]): str(r[UUID]) for r in records if r.get(STATE) == LIVE or str(r["id"]) in finished}
            await self._children(live)
        await self._attempt("ended", self._sweep_ended)
        return self._done()

    async def _children(self, live: dict[str, str]) -> None:
        listed_warehouses = await self._listing("warehouses", partial(warehouses.read_warehouses, self._root, self._so))
        stored, unreadable_warehouses = listed_warehouses or ([], ["the warehouse registry"])
        listed_claims = await self._listing("claims", partial(warehouses.read_claims, self._root, self._so))
        claims, unreadable_claims = listed_claims or ([], ["the bucket claims"])
        # A warehouse record or a claim that cannot be read may be what keeps a bucket from another tenant, so while any
        # cannot, no record is ended and no claim released.
        self._may_release = self._may_end and not unreadable_warehouses and not unreadable_claims
        for record in stored:
            if record.get(STATE) == CREATING:
                await self._attempt(f"warehouse:{record['id']}", partial(self._creating_warehouse, record, live))
        if (listed_roles := await self._listing("roles", partial(roles.every_stored_role, self._root, self._so))) is not None:
            for role in listed_roles[0]:
                if not made_under({PROJECT_UUID: role.project_uuid}, live.get(role.project)):
                    await self._attempt(roles.role_object(role.id), partial(self._orphan_role, role))
        if listed_warehouses is not None:
            gone = {e.removeprefix("warehouse:") for e in self.done.ended}
            standing = [r for r in stored if str(r["id"]) not in gone]
            for claim in claims:
                await self._attempt(f"bucket:{claim['bucket']}", partial(self._claim, claim, live, standing))
        await self._attempt("policies", partial(self._policies, live))

    def _done(self) -> TenancyConverged:
        if self.done.finished or self.done.ended or self.done.swept or self.done.orphans or self.done.released or self.done.failed:
            log.info("tenancy_converged", extra=self.done.model_dump())
        return self.done

    async def _listing[T](self, what: str, read: Callable[[], tuple[list[T], list[str]]]) -> tuple[list[T], list[str]] | None:
        """``read``'s listing, each record it could not read named among the failures; ``None``, named so too, where the
        store did not answer it."""
        try:
            records, unreadable = await run_in_threadpool(read)
        except Exception as exc:  # noqa: BLE001 — a listing the store did not answer stops only what needs it; the next run retries
            log.warning("tenancy_listing_failed", extra={"listing": what, "error": str(exc)[:300]})
            self.done.failed.append(f"{what}: {exc}")
            return None
        for path in unreadable:
            log.error("tenancy_record_unreadable", extra={"listing": what, "record": path})
            self.done.failed.append(f"{path}: cannot be read as a record of {what}; judged on nothing")
        return records, unreadable

    async def _attempt(self, obj: str, step: Callable[[], Awaitable[None]]) -> None:
        try:
            await step()
        except Exception as exc:  # noqa: BLE001 — one object that cannot be converged must not stop the rest; the next run retries it
            log.warning("tenancy_not_converged", extra={"object": obj, "error": str(exc)[:300]})
            self.done.failed.append(f"{obj}: {exc}")

    def _past_bound(self, created: str | datetime | None) -> bool:
        """Whether a record made at ``created`` is older than the bound, so the create that wrote it no longer runs."""
        written = _written(created)
        return written is not None and written < self._older_than

    async def _finish_project(self, record: dict[str, Any]) -> None:
        """Seed and flip a creating project record: idempotent beside a create still running, whose own flip then finds it
        live."""
        await fga_deps.seed_project_admin(self._client, self._settings, ACTOR, project=record, admin=record.get("admin"))
        if await run_in_threadpool(projects.set_live, self._root, self._so, str(record["id"]), str(record[UUID])) is not None:
            self.done.finished.append(f"project:{record['id']}")

    async def _creating_warehouse(self, record: dict[str, Any], live: dict[str, str]) -> None:
        """Finish a creating warehouse whose create no longer runs under its live project creation, holding its claim; end
        one whose project creation is not live (``ChildUnderLiveParent``, ``TickChecksParent``)."""
        if not self._past_bound(record.get("created_at")):
            return
        warehouse_id, project = str(record["id"]), str(record["project"])
        if not made_under(record, live.get(project)):
            if not self._may_end:
                report_orphan(f"warehouse:{warehouse_id}", project=project, project_uuid=str(record[PROJECT_UUID]))
                self.done.orphans.append(f"warehouse:{warehouse_id}")
                return
            await fga_deps.end_object(self._client, self._settings, object_of_record(WAREHOUSE, record), token=None)
            await run_in_threadpool(warehouses.end_warehouse, self._root, self._so, record)
            log.warning("warehouse_of_a_dead_creation_ended", extra={"warehouse": warehouse_id, "project": project, "bucket": record.get("bucket")})
            self.done.ended.append(f"warehouse:{warehouse_id}")
            return
        claim = await run_in_threadpool(partial(warehouses.bucket_claim, self._root, self._so, bucket=str(record["bucket"])))
        if claim is None or claim.get(warehouses.PURGING) or not made_under(claim, record[PROJECT_UUID]):
            return
        ns = self._connections.connection(self._settings, root=str(record["root_uri"]), endpoint=record.get("endpoint") or None)
        await run_in_threadpool(warehouses.ensure_catalog_row, ns, warehouse_id)
        project_record = {"id": project, UUID: record[PROJECT_UUID]}
        await fga_deps.seed_warehouse(self._client, self._settings, None, warehouse=record, project=project_record, actor=ACTOR)
        if await run_in_threadpool(warehouses.set_live, self._root, self._so, warehouse_id, str(record[UUID])) is not None:
            self.done.finished.append(f"warehouse:{warehouse_id}")

    async def _sweep_ended(self) -> None:
        """Revoke what stands on, and names, each ended creation's object; forget it once a sweep past the bound found
        nothing (``DeadObjectsSwept``)."""
        listed = await self._listing("ended", partial(control_records.read_ended, self._root, self._so))
        for ended in listed[0] if listed is not None else []:
            obj = object_of_record(ended.kind, {UUID: ended.uuid})
            revoked = await fga_deps.end_object(self._client, self._settings, obj, token=None)
            if revoked:
                self.done.swept[obj] = len(revoked)
            swept = _written(ended.swept_at)
            if revoked or swept is None:
                await run_in_threadpool(partial(control_records.mark_swept, self._root, self._so, ended, now=self._now))
            elif swept < self._older_than:
                await run_in_threadpool(control_records.forget_ended, self._root, self._so, ended)

    async def _claim(self, claim: dict[str, str], live: dict[str, str], stored: list[dict[str, Any]]) -> None:
        """Release a claim a dead creation took that no record names, once it is older than the bound, on a bucket left
        holding NOTHING (ADR 0241 §10 Q6a): where what stands in it is only what a create writes, the tick takes the claim
        for itself (the same ``purging`` stamp a purge takes), removes exactly those objects, lists again, and releases
        only if the bucket is empty. Anything else in it keeps the claim, reported as ``bucket:<name>``.

        A claim a warehouse delete's purge stamped whose record is gone is released: the record goes only after the bytes."""
        bucket, project = claim["bucket"], claim["project"]
        project_uuid = claim[PROJECT_UUID]
        holder = warehouses.purge_holder("catalog", bucket, project_uuid)
        # A stamp of the tick's own is a run that died removing what the create wrote, which this run resumes.
        if (stamp := claim.get(warehouses.PURGING)) and stamp != holder:
            await self._finish_purge(bucket, stamp, stored)
            return
        if made_under(claim, live.get(project)):
            return
        if any(r.get("bucket") == bucket for r in stored):
            return
        report_orphan(f"bucket:{bucket}", project=project, project_uuid=project_uuid)
        self.done.orphans.append(f"bucket:{bucket}")
        if not self._may_release:
            return
        if (claimed := _written(claim.get(warehouses.CLAIMED_AT))) is None:
            log.error("claim_without_claimed_at", extra={"bucket": bucket})
            raise InternalError(f"the claim on bucket {bucket!r} names no time it was taken; every claim names one, so it was not written by this catalog")
        if claimed >= self._older_than:
            return
        if foreign := await run_in_threadpool(partial(warehouses.written_by_others, bucket, self._so, warehouse_id=claim["warehouse_id"])):
            log.error("bucket_claim_kept", extra={"bucket": bucket, "project": project, "project_uuid": project_uuid, "found": foreign})
            return
        hold = await run_in_threadpool(
            partial(warehouses.take_for_purge, self._root, self._so, bucket=bucket, project=project, project_uuid=project_uuid, holder=holder)
        )
        if hold is None:
            return
        trail = {"subject": ACTOR, "resource": f"bucket:{bucket}", "project": project, "project_uuid": project_uuid, "bucket": bucket}
        audit(AuditAction.BUCKET_PURGE, AuditOutcome.ALLOW, **trail, dead_tenant=True, stage="claim_taken")
        try:
            removed = await run_in_threadpool(
                partial(warehouses.purge_bucket, self._root, self._so, hold, only=warehouses.CREATE_PREFIXES, remove_bucket=False)
            )
            self._connections.forget(f"s3://{bucket}")
            empty = not await run_in_threadpool(warehouses.holds_anything, bucket, self._so)
            await run_in_threadpool(partial(warehouses.end_purge, self._root, self._so, hold, released=RELEASED_UNBORN if empty else None))
        except Exception as exc:
            audit(AuditAction.BUCKET_PURGE, AuditOutcome.FAILURE, **trail, dead_tenant=True, stage="claim_taken", error=type(exc).__name__)
            raise
        audit(
            AuditAction.BUCKET_PURGE,
            AuditOutcome.SUCCESS,
            **trail,
            dead_tenant=True,
            stage="claim_released" if empty else "bytes_deleted",
            objects_purged=removed,
        )
        if not empty:
            # Something landed beside what the create wrote while the tick removed it: the claim is kept, given back.
            log.error("bucket_claim_kept", extra={"bucket": bucket, "project": project, "project_uuid": project_uuid, "found": "objects after removal"})
            return
        log.warning("bucket_claim_released", extra={"bucket": bucket, "project": project, "project_uuid": project_uuid, "removed": removed})
        self.done.released.append(bucket)

    async def _finish_purge(self, bucket: str, stamp: str, stored: list[dict[str, Any]]) -> None:
        """Release a claim a warehouse delete's purge stamped once that warehouse's record is gone: the delete removes its
        record only after the bytes, so a purge that died between its record and its claim's release left nothing of the
        warehouse in the bucket. A purge whose record stands is the delete's to resume; any other stamp is left as it is."""
        kind, _, rest = stamp.partition(":")
        warehouse_id, _, uuid = rest.rpartition("@")
        if kind != "warehouse" or any(r["id"] == warehouse_id and r.get(UUID) == uuid for r in stored):
            return
        hold = warehouses.PurgeHold(bucket=bucket, holder=stamp)
        if await run_in_threadpool(partial(warehouses.end_purge, self._root, self._so, hold, released="bucket_purged")):
            log.warning("bucket_purge_finished", extra={"bucket": bucket, "holder": stamp})
            self.done.released.append(bucket)

    async def _policies(self, live: dict[str, str]) -> None:
        """Report each project policy a creation that is not live set (a set racing its project's delete). It is not ended:
        a write under the project's id here could only be unconditional, and would take a policy the tenant holding the id
        set meanwhile. It governs only a bucket whose claim names the creation that set it
        (``maintenance_policies.governing_policy``), no door reads or merges onto it, and the next set replaces it."""
        listed = await self._listing("policies", partial(maintenance_policies.read_policies, self._root, self._so))
        for record in listed[0] if listed is not None else []:
            project = str(record.get("id"))
            if record.get("kind") != PROJECT or made_under(record, live.get(project)):
                continue
            report_orphan(f"policy:project:{project}", project=project, project_uuid=str(record.get(PROJECT_UUID)))
            self.done.orphans.append(f"policy:project:{project}")

    async def _orphan_role(self, role: roles.StoredRole) -> None:
        obj = roles.role_object(role.id)
        report_orphan(obj, project=role.project, project_uuid=role.project_uuid)
        self.done.orphans.append(obj)
        if not self._may_end:
            return
        # Its create wrote it under a project creation that is not live: its edge names that dead creation and it has no
        # assignee anyone resolves, so only its record is ended, and its name is free under the project.
        await run_in_threadpool(roles.end_role_record, self._root, self._so, role)
        self.done.ended.append(obj)


async def converge(
    settings: Settings,
    client: OpenFgaClient | None,
    *,
    bound: timedelta,
    connections: NamespaceConnections,
    now: datetime | None = None,
) -> TenancyConverged:
    """Finish or end every creating record, sweep every ended creation, and report, end and release what a dead creation
    left; what was done. Never raises: what stopped it is named among the failures (ADR 0241 §10 Q3).

    Idempotent and safe beside every door: a finish writes what is absent and flips only a record still creating with its
    uuid, and an end, a release or a forgetting acts only past ``bound``, which a request's run does not reach, judged by
    ``now``, the catalog's clock. ``connections`` are the process's namespace connections: a finished warehouse's catalog
    row is ensured through them, and one rooted at a bucket whose index the tick removed is dropped.
    """
    convergence = _Convergence(settings, client, bound=bound, now=now or datetime.now(UTC), connections=connections)
    try:
        return await convergence.run()
    except Exception as exc:  # noqa: BLE001 — the tick's and the boot's later steps run whatever stopped this one (ADR 0241 §10 Q3)
        log.error("tenancy_not_converged", extra={"error": str(exc)[:300]}, exc_info=True)
        convergence.done.failed.append(f"tenancy: {exc}")
        return convergence.done
