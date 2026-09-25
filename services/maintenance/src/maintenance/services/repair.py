"""Revoking authorization for objects the control plane no longer has ([[LH-061]]'s repair half).

**DELETES TUPLES AND NOTHING ELSE. It never touches a byte, a registry record or a catalog table**,
and that bound is the whole reason this is safe to arm against a live estate. Owner ruling 2026-09-19
(`docs/DECISIONS.md`) overturning the standing "No — not yet" deferral on a write-capable reconcile:
a repair pass over the drift report, dry-run by DEFAULT with deletion opt-in. `rebuild.py` is the
additive half; this is the half that can destroy something.

WHAT IT ACTS ON is one fact wearing three category names: **the object itself is gone.**
`ghost_projects`, `ghost_warehouses` and `ghost_tables` are all
:class:`~maintenance.services.reconcile.GhostObject` — tuples on an FGA object no registry record
names — so revoking them removes nobody's access to anything: there is no door to open and no bytes
behind it. Measured live 2026-09-20, `ghost_tables` alone stands at 1,033, residue of the warehouse
cascade [[LH-148]] fixed at source — a producer already closed, leaving a backlog nothing could clear.

`orphaned_annotation_tasks` IS NOT A FOURTH GHOST, and the distinction is what this module turns on.
There the annotation project still EXISTS; only its `tenant` edge names a project that does not.
`revoke_object_tuples` is all-or-nothing by OBJECT, so using it would destroy a live object's whole
authorization to clear one stale edge. So it gets the other seam: an EXACT-TUPLE delete, planned and
reported separately, because "removed all authz on a dead object" and "cut one stale edge on a live
one" are different acts and an operator must be able to tell them apart in the audit stream. The
finding carries both ends (`annotation_project` and `tenant`), so the tuple is fully determined and
needs no lookup — which is also why this cannot widen: it can only ever delete the one edge the report
found dangling.

WHAT IT REFUSES, BY NAME RATHER THAN BY OMISSION. `ungoverned_tables` is the INVERSE shape and the
reason this module has a refusal list at all: a real table, real bytes, no tuples. A pass that
"repaired drift" by acting on it would delete live data to close an authorization gap — the single
worst thing a cleaner can do. `unbound_namespaces` and `unreferenced_projects` are real records, and
`orphan_buckets`/`orphan_files`/`orphaned_trash` are bytes whose deletion needs storage evidence this
pass does not gather. Each is REFUSED with a reason rather than silently skipped, so a reader can see
the pass considered it and declined, which is what distinguishes a bounded tool from an unfinished one.

THE REFUSALS ARE NOT A BACKLOG THIS MODULE WILL GROW INTO. The storage tier is a different kind of
work — it needs a listing, a floor and an age, and it can destroy something unrecoverable — so it is a
separate pass with its own evidence, not a flag on this one.

A REVOKE IS JUSTIFIED BY AN ABSENCE, SO A PARTIAL READ JUSTIFIES NOTHING. Every category here is
derived by subtraction — a tuple whose id appears in no registry record — which is evidence only while
the listing it was measured against was COMPLETE. `_run_category` refuses a category whose source
reported an error, so an outage produces no findings; a partial read travels on a different channel
and does not (`_tables_across` returns unreadable roots beside the rows it did get, filed as
`IncompleteScan`, with `tables_error` still None). One transient manifest failure on one warehouse
root would therefore classify every table under it as a ghost. `_SUBTRACTED_FROM` names the source
behind each category and the pass refuses that category — by name, with the partial source in the
reason — when it was read in part. The check is PER CATEGORY: this estate carries a storage-tier
`IncompleteScan` on most ticks, and none of those feed a revoke.

THE TOMBSTONE SWEEP NEEDS NO SUCH GUARD, and the contrast is the reason this one does. That pass
PROBES each location and refuses unless the bytes are provably gone, so its decision rests on a direct
observation rather than on an absence from a listing. Here there is nothing to probe: an FGA object
that does not exist cannot be asked whether it exists.

OFF AND DRY-RUN BY DEFAULT. `drift_repair_dry_run` defaults TRUE, which is stricter than the trash
purge's default and deliberately so: the purge's targets are already-expired records on a maintained
root, while this writes to the authorization store, where a wrong revoke is felt by a person holding a
grant rather than by a reclaimer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from service_kit.governed import fga


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings
    from maintenance.services.reconcile import ReconcileReport


log = logging.getLogger(__name__)


#: The report categories whose findings name an FGA object that no longer exists. Held as data rather
#: than branched in code so the refusal list below can be derived as "everything else that was found",
#: which is what keeps a NEW drift category from defaulting into being deletable.
_REVOCABLE: tuple[str, ...] = ("ghost_projects", "ghost_warehouses", "ghost_tables")

#: Categories repaired by deleting ONE exact tuple instead of revoking an object — the object is alive
#: and keeps every other grant it holds. Its own list rather than a flag on `_REVOCABLE` because the
#: two acts differ in blast radius, and a reader must not have to infer which one a category gets.
_EDGE_ONLY: tuple[str, ...] = ("orphaned_annotation_tasks",)

#: The SOURCE each repairable category's "it is gone" inference is subtracted from, keyed by category.
#:
#: A revoke is justified by an ABSENCE — a tuple whose id appears in no registry record — and an
#: absence is only evidence when the listing it was measured against was complete. `_run_category`
#: already refuses a category whose source reported an ERROR, so an outage yields no findings; a
#: PARTIAL read travels on a different channel and does not. `_tables_across` returns its unreadable
#: roots beside the rows it did get and `_build_sources` files them as
#: `IncompleteScan(source="catalog:tables:<root>")` while `tables_error` stays None, so the category
#: runs against a record set missing exactly those roots. One transient manifest failure would then
#: classify every table under that warehouse as a ghost and strip its authorization.
#:
#: Matched as a PREFIX because the catalog sources are per-root (`catalog:tables:s3://acme-wh`) while
#: the registry sources are not (`registry:projects`).
#:
#: `fga:tuples` is deliberately absent. A truncated tuple scan finds FEWER objects carrying tuples,
#: so it under-reports ghosts — the safe direction, and guarding it would refuse work for a partial
#: read that cannot produce a false positive.
_SUBTRACTED_FROM: dict[str, str] = {
    "ghost_projects": "registry:projects",
    "ghost_warehouses": "registry:warehouses",
    "ghost_tables": "catalog:tables",
    # The edge cut reads the SAME projects registry: a skipped record makes a live tenant look retired,
    # and the cut would strip the `tenant` edge off an annotation project whose owner still exists.
    "orphaned_annotation_tasks": "registry:projects",
}


#: Why each non-revocable category is refused. Phrased as what the finding IS, because the reason a
#: pass must not delete it is a property of the object rather than of this module's scope.
_REFUSED: dict[str, str] = {
    "absent_datasets": "a catalog RECORD whose location holds no bytes — the fix is a catalog write and it is a decision: the bytes may be restorable, and the record is the only thing that still says where they belonged",
    "unregistered_datasets": "a REAL dataset on storage that no catalog record names — the resolution is REGISTRATION, never deletion; deleting it would destroy live rows to close a bookkeeping gap",
    "ungoverned_tables": "a REAL table carrying no tuples — deleting it would destroy live data to close an authorization gap; it needs a grant, not a revoke",
    "unbound_namespaces": "a real top-level namespace missing a warehouse BINDING record — it needs binding, and the data under it is live",
    "unreferenced_projects": "a real project record holding no tuples — it needs an admin grant, which is a decision a person makes (see `rebuild.py`)",
    "orphan_buckets": "storage nothing claims — deleting bytes needs a listing, a floor and an age this pass does not gather",
    "orphan_files": "storage nothing claims — same as `orphan_buckets`, and the ordinary sweep clears most of it",
    "orphaned_trash": "a trash record naming an unmaintained root — it is the only remaining POINTER to those bytes, so dropping it strands them silently",
    "dangling_bindings": "a binding record pointing at a missing warehouse — a registry write, not an authz one",
}


class RevokedObject(BaseModel):
    """One object this pass would revoke, or revoked. NAMED, never counted — it is an authorization
    change, and which object lost how many grants is the entire audit trail."""

    fga_object: str
    #: How many tuples the report saw on it, so a reader can tell a stray edge from a whole tenant.
    tuples: int
    #: The drift category that justifies it, so the claim is checkable without this module.
    justified_by: str


class CutEdge(BaseModel):
    """One exact tuple this pass would delete, or deleted — a dead edge on a LIVE object."""

    user: str
    relation: str
    object: str
    justified_by: str


class RepairReport(BaseModel):
    enabled: bool = False
    dry_run: bool = True
    revoked: list[RevokedObject] = Field(default_factory=list)
    #: Exact tuples cut from objects that are still alive. Separate from `revoked` on purpose: the two
    #: differ in blast radius, and a single list would let a reader mistake one for the other.
    edges_cut: list[CutEdge] = Field(default_factory=list)
    #: Categories the report found and this pass declined, with why. Present even when empty-handed:
    #: "considered and refused" and "never looked" are different facts about a cleaner.
    refused: dict[str, str] = Field(default_factory=dict)
    #: Eligible objects beyond `max_per_tick`, so a truncated pass cannot read as a complete one.
    capped: int = 0
    error: str | None = None


def _partial(report: ReconcileReport, category: str) -> str | None:
    """The reason ``category``'s inference cannot be trusted this tick, or None.

    Returns the SOURCE that was read partially rather than a boolean, because the refusal has to name
    it: a tick that revoked nothing must be distinguishable from an estate with nothing to revoke.
    """
    prefix = _SUBTRACTED_FROM.get(category)
    if prefix is None:
        return None
    hit = [scan for scan in report.incomplete or [] if scan.source.startswith(prefix)]
    return None if not hit else ", ".join(f"{scan.source} ({scan.reason})" for scan in hit)


def plan_repair_edges(report: ReconcileReport) -> tuple[list[RevokedObject], list[CutEdge], dict[str, str]]:
    """The full plan: whole-object revokes, exact-edge cuts, and the categories refused with reasons."""
    planned, refused = plan_repair(report)
    edges: list[CutEdge] = []
    if (why := _partial(report, "orphaned_annotation_tasks")) is not None:
        refused["orphaned_annotation_tasks"] = f"the registry this tenant check subtracts from was read only in part — {why}"
    else:
        edges = [
            CutEdge(
                user=f"project:{finding.tenant}",
                relation="tenant",
                object=f"annotation_project:{finding.annotation_project}",
                justified_by="orphaned_annotation_tasks",
            )
            for finding in report.orphaned_annotation_tasks or []
        ]
    return planned, edges, refused


def plan_repair(report: ReconcileReport) -> tuple[list[RevokedObject], dict[str, str]]:
    """The objects whose tuples are revocable, and the categories refused with their reason.

    SCOPED TO WHAT THE REPORT FOUND, never to a store scan: the justification for revoking is the
    report's own finding that no registry record names the object, so a second, independent read could
    revoke an object the report never classified.
    """
    planned: list[RevokedObject] = []
    refused = {name: why for name, why in _REFUSED.items() if getattr(report, name, None)}
    for category in _REVOCABLE:
        findings = getattr(report, category, []) or []
        if not findings:
            continue
        # PER CATEGORY, never per report. This estate's reconcile carries a storage-tier IncompleteScan
        # on most ticks (`storage:datasets`, depth limits), and none of those feed a revoke — stopping
        # the whole pass on any incompleteness would refuse every tick for a reason that cannot produce
        # a false ghost.
        if (why := _partial(report, category)) is not None:
            refused[category] = f"the listing this absence was measured against was read only in part — {why}"
            continue
        for finding in findings:
            obj = getattr(finding, "fga_object", None) or f"annotation_project:{finding.annotation_project}"
            planned.append(RevokedObject(fga_object=obj, tuples=getattr(finding, "tuples", 0), justified_by=category))
    return planned, refused


async def repair_drift(settings: MaintenanceSettings, *, report: ReconcileReport, fga_client: Any) -> RepairReport:
    """Revoke the tuples of every object the report found gone.

    Degrades rather than fails, the same contract `rebuild.py` carries: a store that will not accept
    the deletes leaves `error` set and the tick continues, because a reconcile tick that 500s over a
    failed repair is worse than one reporting drift it could not clear — the report is what everything
    else is gated on.
    """
    if not settings.drift_repair_enabled or fga_client is None:
        return RepairReport(enabled=settings.drift_repair_enabled, dry_run=settings.drift_repair_dry_run)
    planned, edges, refused = plan_repair_edges(report)
    out = RepairReport(enabled=True, dry_run=settings.drift_repair_dry_run, refused=refused)
    # CAPPED ON THE REVOKES ONLY. An edge cut removes exactly one tuple from a live object, so a
    # handful of them cannot be the unreviewable batch the cap exists to prevent — and counting them
    # against it would let a backlog of ghosts starve the smaller, safer repair indefinitely.
    out.capped = max(0, len(planned) - settings.drift_repair_max_per_tick)
    planned = planned[: settings.drift_repair_max_per_tick]
    if out.dry_run:
        out.revoked, out.edges_cut = planned, edges
        return out
    done: list[RevokedObject] = []
    for target in planned:
        try:
            await fga.revoke_object_tuples(fga_client, target.fga_object, actor=settings.catalog_service_identity, origin="drift_repair")
        except Exception as exc:  # noqa: BLE001 — see the docstring; one object must not fail the tick
            log.warning("drift_repair_object_failed", extra={"object": target.fga_object, "error": str(exc)})
            out.error = str(exc)
            continue
        done.append(target)
    out.revoked = done
    cut: list[CutEdge] = []
    for edge in edges:
        try:
            await fga.delete_tuples(
                fga_client,
                [fga.ClientTuple(user=edge.user, relation=edge.relation, object=edge.object)],
                actor=settings.catalog_service_identity,
                origin="drift_repair",
            )
        except Exception as exc:  # noqa: BLE001 — same contract as the revokes above
            log.warning("drift_repair_edge_failed", extra={"object": edge.object, "error": str(exc)})
            out.error = str(exc)
            continue
        cut.append(edge)
    out.edges_cut = cut
    return out
