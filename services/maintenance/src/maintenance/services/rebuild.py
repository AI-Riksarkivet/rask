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

**STRUCTURE IS REBUILT; A PERMISSION IS NEVER RE-GRANTED — and that line is upstream's, not ours.**
Lakekeeper's `lakekeeper openfga reconcile` solves the same problem for the same pair of stores, and
its scope is "the parent/child edges between server, projects, warehouses, namespaces, tables, views,
and roles", with "ownership tuples, grants, role assignments, bootstrap admin tuples, and
authorization-model bookkeeping ... left alone"
(https://docs.lakekeeper.io/docs/latest/authorization-openfga/). This module holds the same line.

THE REASON IT IS THE RIGHT LINE HERE TOO, and it is specific rather than deference: **the registry does
not record REVOCATION.** A project record carries ``created_by`` — 93 of 93, measured on the live
control root 2026-09-19 — but that field says who created the tenant, not who may administer it today.
An admin deliberately removed leaves the record untouched, so "the record justifies this grant" is
FALSE for exactly the person an operator took care to remove, and re-granting them is a privilege
restoration wearing a repair's name. A structural edge cannot do that: it says where an object lives
and confers nothing on its own.

So the split is:

* a warehouse record is ``{id, bucket, root_uri, project, status, created_at}`` — the tenancy pointer
  ``project:<p> project warehouse:<id>`` is STRUCTURAL and is rebuilt;
* a project holding no tuples at all is REPORTED and not repaired. It needs an admin grant, which is a
  decision a person makes; this names the tenant and the ``created_by`` the record holds, so that
  decision is one call away and is still a decision.

THE BOUND, STATED SO NOBODY READS THIS AS A BACKUP. Nothing here restores access. Grants, ownership,
role assignments, team edges, per-table owners and the cascade identities (catalog CONFIG, in no record
at all) come back from nothing. What comes back is the shape of the estate.

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


def _needs_an_admin(record: dict[str, str]) -> str:
    """Why this tenant cannot be repaired here, phrased so the next step is obvious.

    NOT A GRANT, DELIBERATELY. `seed_project_admin` writes `user:<created_by> admin project:<id>` at
    create time and this could mirror it — the field is there. It does not, because the registry does
    not record REVOCATION: `created_by` names who created the tenant, never who may administer it
    today, so re-asserting it re-grants exactly the person an operator may have taken care to remove.
    The finding names the id the record holds so a human can make that call in one request.
    """
    creator = record.get("created_by")
    return (
        f"holds no tuples and needs an admin grant — a decision, not a repair. The record's `created_by` is {creator!r}; "
        "it records who CREATED the tenant, not who may administer it now, so this pass will not re-grant it."
        if creator
        else "holds no tuples and the record carries no `created_by`, so not even a candidate admin is recorded"
    )


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
    stranded = {finding.id for finding in report.unreferenced_projects}
    planned: list[RebuiltTuple] = []
    unjustified: dict[str, str] = {}

    for record in sources.project_records or []:
        if (project := record.get("id")) in stranded:
            unjustified[f"project:{project}"] = _needs_an_admin(record)

    # THE STRUCTURAL HALF, and the only half that writes. Scoped to warehouses under a project the
    # report found stranded, because that is where the pointer is provably gone: a whole-estate
    # re-assertion would write an authorization row per warehouse per tick forever and bury the one
    # that mattered.
    for record in sources.warehouse_records or []:
        if record.get("project") not in stranded:
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
