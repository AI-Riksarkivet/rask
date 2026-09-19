"""Rebuilding authorization tuples the control-plane registries still justify ([[LH-061]]).

**ADDITIVE ONLY. This module writes tuples and never deletes one**, which is what makes it safe to
run against a live store: every tuple it writes is one the registry record already justifies, so it
cannot widen access beyond what the control plane itself recorded. Owner ruling 2026-09-19
(`docs/DECISIONS.md`) overturning the standing "No — not yet" deferral on a write-capable reconcile.

WHY IT EXISTS. `reconcile.py` detects `unreferenced_projects` — a project record holding no tuples at
all — and could do nothing about it. That state is not cosmetic: the project-admin tuple is what makes
a tenant self-sustaining (every later warehouse, namespace and table grant cascades from it), so a
tenant that loses it is one nobody can administer, grant on, or delete. Until now the estate could
detect that and not recover from it, and "the tuple estate cannot be rebuilt after a loss" is a
resilience gap rather than a missing convenience.

**WHAT THE REGISTRIES ACTUALLY JUSTIFY, measured rather than assumed, and it is less than the row
claimed.** Read from the live control root 2026-09-19:

* a project record is ``{id, created_at, created_by, protected}`` — 93 of 93 carry ``created_by``, so
  ``user:<created_by> admin project:<id>`` is exactly the tuple `seed_project_admin` writes at create
  time and is fully recoverable;
* a warehouse record is ``{id, bucket, root_uri, project, status, created_at}`` and carries **NO
  ``created_by``** — 97 of 97. So the creator's ``owner`` grant on a warehouse is NOT recoverable from
  the registry, because the registry never recorded who it belonged to. Only the tenancy pointer
  ``project:<p> project warehouse:<id>`` is justified, and that is the one this writes.

THE BOUND, STATED SO NOBODY READS THIS AS A BACKUP. What is rebuilt is the SEED: a tenant becomes
administrable again and its warehouses point at it again. Grants made after creation — other admins,
role assignments, team edges, per-table owners, the cascade identities (which come from catalog CONFIG
and not from any record) — are not in these registries and no amount of reading them back recovers a
single one. A rebuilt estate is one an admin can start repairing, not one that is repaired.

OFF AND DRY-RUN BY DEFAULT, the same two flags the floor raise carries and for the same reason: "does
this estate want the behaviour" and "does this tick act" are different questions, and an operator
turning it on to SEE what it would write must not thereby write to the authorization store.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from service_kit.governed import fga


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.reconcile import ReconcileReport, Sources

log = logging.getLogger(__name__)


class RebuiltTuple(BaseModel):
    """One tuple this pass would write, or wrote. NAMED, never counted — it is an authorization
    change, so which subject gained which rung on which object is the entire audit trail."""

    user: str
    relation: str
    object: str
    #: Which registry record justifies it, so a reader can check the claim without this module.
    justified_by: str


class RebuildReport(BaseModel):
    enabled: bool = False
    dry_run: bool = True
    written: list[RebuiltTuple] = Field(default_factory=list)
    #: Records that justify no tuple, with why. A project with no `created_by` is the shape that
    #: matters: the record exists, the tenant is unadministrable, and nothing here can fix it.
    unjustified: dict[str, str] = Field(default_factory=dict)
    #: Eligible records beyond `max_per_tick`, reported so a truncated pass cannot read as a complete one.
    capped: int = 0
    error: str | None = None


def _project_seed(record: dict[str, str]) -> RebuiltTuple | None:
    """The one tuple a project record justifies, or ``None`` when it justifies none.

    Mirrors `catalog.api.fga_deps.seed_project_admin` exactly — `user:<sub> admin project:<id>`. A
    rebuild that invented a different shape would restore a tenant the create door could not have
    produced, and the difference would surface as an authorization decision nobody can explain.
    """
    project, creator = record.get("id"), record.get("created_by")
    if not project or not creator:
        return None
    return RebuiltTuple(user=f"user:{creator}", relation="admin", object=f"project:{project}", justified_by=f"_projects/{project}.json")


def _warehouse_tenancy(record: dict[str, str]) -> RebuiltTuple | None:
    """The warehouse's tenancy pointer — relation ``project``, NOT ``parent``.

    `model.fga` defines `project: [project]` on the warehouse type, and `parent` is a relation that
    type does not declare; writing it makes OpenFGA reject the whole seed with a 503. The catalog's
    `seed_warehouse` writes this relation directly for the same reason, and its being a plain pointer
    rather than a hierarchy edge is why rebuilding it does not touch
    `test_invariants.py::test_only_the_sanctioned_writers_seed_a_hierarchy_edge`, whose closed set of
    two callers is about `parent`/`child` pairs.
    """
    warehouse, project = record.get("id"), record.get("project")
    if not warehouse or not project:
        return None
    return RebuiltTuple(user=f"project:{project}", relation="project", object=f"warehouse:{warehouse}", justified_by=f"_warehouses/{warehouse}.json")


def plan_rebuild(report: ReconcileReport, sources: Sources) -> tuple[list[RebuiltTuple], dict[str, str]]:
    """The tuples the registries justify for the objects the report found UNGOVERNED, and the records
    that justify none.

    SCOPED TO THE REPORT'S FINDINGS rather than to every record, and that is the difference between a
    repair and a rewrite. `write_tuples` is idempotent, so re-asserting all 93 project seeds every tick
    would be harmless and also unreadable — the audit stream would carry an authorization write per
    tenant per tick forever, and the one that mattered would be indistinguishable from the noise.
    """
    unreferenced = {finding.id for finding in report.unreferenced_projects}
    planned: list[RebuiltTuple] = []
    unjustified: dict[str, str] = {}

    for record in sources.project_records or []:
        if (project := record.get("id")) not in unreferenced:
            continue
        if (seed := _project_seed(record)) is None:
            unjustified[f"project:{project}"] = "the record carries no `created_by`, so no grant is justified by it"
            continue
        planned.append(seed)

    # A warehouse whose tenancy pointer is missing shows up as its project holding no tuples on it;
    # the report has no category for that, so the pointer is asserted for any warehouse belonging to a
    # project this pass is reviving. A warehouse under a healthy project is left alone.
    revived = {t.object.removeprefix("project:") for t in planned}
    for record in sources.warehouse_records or []:
        if record.get("project") not in revived:
            continue
        if (pointer := _warehouse_tenancy(record)) is None:
            unjustified[f"warehouse:{record.get('id')}"] = "the record names no project, so its tenancy is unknown"
            continue
        planned.append(pointer)
    return planned, unjustified


async def rebuild_tuples(settings: MaintenanceSettings, *, report: ReconcileReport, sources: Sources, fga_client: Any) -> RebuildReport:
    """Write the tuples the registries justify for objects the report found ungoverned.

    Degrades rather than fails: a store that will not accept the writes leaves `error` set and the
    tick continues. A reconcile tick that 500s because a repair failed is a worse outcome than one
    that reports drift it could not fix — the report is the thing everything else is gated on.
    """
    out = RebuildReport(enabled=settings.tuple_rebuild_enabled, dry_run=settings.tuple_rebuild_dry_run)
    if not settings.tuple_rebuild_enabled or fga_client is None:
        return out
    planned, out.unjustified = plan_rebuild(report, sources)
    out.capped = max(0, len(planned) - settings.tuple_rebuild_max_per_tick)
    planned = planned[: settings.tuple_rebuild_max_per_tick]
    if not planned or out.dry_run:
        out.written = planned
        return out
    try:
        await fga.write_tuples(
            fga_client,
            [fga.ClientTuple(user=t.user, relation=t.relation, object=t.object) for t in planned],
            actor=settings.catalog_service_identity,
            origin="registry_rebuild",
        )
    except Exception as exc:  # noqa: BLE001 — a failed repair must not fail the report it is gated on
        log.warning("tuple_rebuild_failed", extra={"error": str(exc), "planned": len(planned)})
        out.error = str(exc)
        return out
    out.written = planned
    return out
