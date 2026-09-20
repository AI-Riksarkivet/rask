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

`orphaned_annotation_tasks` LOOKS like a fourth and is not, which is the distinction this module turns
on. There the annotation project still EXISTS; only its `tenant` edge names a project that does not.
`revoke_object_tuples` is all-or-nothing by object, so using it here would destroy the authorization of
a live object to clear one stale edge. Deleting that single edge is a different seam and a different
piece of work, so it is REFUSED by name below rather than folded in for symmetry.

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

OFF AND DRY-RUN BY DEFAULT. `drift_repair_dry_run` defaults TRUE, which is stricter than the trash
purge's default and deliberately so: the purge's targets are already-expired records on a maintained
root, while this writes to the authorization store, where a wrong revoke is felt by a person holding a
grant rather than by a reclaimer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
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

#: Why each non-revocable category is refused. Phrased as what the finding IS, because the reason a
#: pass must not delete it is a property of the object rather than of this module's scope.
_REFUSED: dict[str, str] = {
    "ungoverned_tables": "a REAL table carrying no tuples — deleting it would destroy live data to close an authorization gap; it needs a grant, not a revoke",
    "unbound_namespaces": "a real top-level namespace missing a warehouse BINDING record — it needs binding, and the data under it is live",
    "unreferenced_projects": "a real project record holding no tuples — it needs an admin grant, which is a decision a person makes (see `rebuild.py`)",
    "orphan_buckets": "storage nothing claims — deleting bytes needs a listing, a floor and an age this pass does not gather",
    "orphan_files": "storage nothing claims — same as `orphan_buckets`, and the ordinary sweep clears most of it",
    "orphaned_trash": "a trash record naming an unmaintained root — it is the only remaining POINTER to those bytes, so dropping it strands them silently",
    "dangling_bindings": "a binding record pointing at a missing warehouse — a registry write, not an authz one",
    "orphaned_annotation_tasks": (
        "the annotation project still EXISTS and only its `tenant` edge dangles, so revoking every tuple on it "
        "would destroy authz for a live object to clear one stale edge — it needs that single edge deleted, which is "
        "a different seam from `revoke_object_tuples`"
    ),
}


class RevokedObject(BaseModel):
    """One object this pass would revoke, or revoked. NAMED, never counted — it is an authorization
    change, and which object lost how many grants is the entire audit trail."""

    fga_object: str
    #: How many tuples the report saw on it, so a reader can tell a stray edge from a whole tenant.
    tuples: int
    #: The drift category that justifies it, so the claim is checkable without this module.
    justified_by: str


class RepairReport(BaseModel):
    enabled: bool = False
    dry_run: bool = True
    revoked: list[RevokedObject] = Field(default_factory=list)
    #: Categories the report found and this pass declined, with why. Present even when empty-handed:
    #: "considered and refused" and "never looked" are different facts about a cleaner.
    refused: dict[str, str] = Field(default_factory=dict)
    #: Eligible objects beyond `max_per_tick`, so a truncated pass cannot read as a complete one.
    capped: int = 0
    error: str | None = None


def plan_repair(report: ReconcileReport) -> tuple[list[RevokedObject], dict[str, str]]:
    """The objects whose tuples are revocable, and the categories refused with their reason.

    SCOPED TO WHAT THE REPORT FOUND, never to a store scan: the justification for revoking is the
    report's own finding that no registry record names the object, so a second, independent read could
    revoke an object the report never classified.
    """
    planned: list[RevokedObject] = []
    for category in _REVOCABLE:
        for finding in getattr(report, category, []) or []:
            obj = getattr(finding, "fga_object", None) or f"annotation_project:{finding.annotation_project}"
            planned.append(RevokedObject(fga_object=obj, tuples=getattr(finding, "tuples", 0), justified_by=category))
    refused = {name: why for name, why in _REFUSED.items() if getattr(report, name, None)}
    return planned, refused


def repair_drift_sync(
    settings: MaintenanceSettings,
    *,
    report: ReconcileReport,
    revoke: Callable[[str], Sequence[object]],
) -> RepairReport:
    """The pass's decision logic, with the revoke handed in.

    Separated from :func:`repair_drift` so the ARMED path is testable without an OpenFGA client: the
    thing worth pinning here is which objects get revoked and which are refused, and a test that had to
    stand up a store to ask that question would be run rarely and trusted less.
    """
    out = RepairReport(enabled=settings.drift_repair_enabled, dry_run=settings.drift_repair_dry_run)
    if not settings.drift_repair_enabled:
        return out
    planned, out.refused = plan_repair(report)
    out.capped = max(0, len(planned) - settings.drift_repair_max_per_tick)
    planned = planned[: settings.drift_repair_max_per_tick]
    if not planned or out.dry_run:
        out.revoked = planned
        return out
    done: list[RevokedObject] = []
    for target in planned:
        try:
            revoke(target.fga_object)
        except Exception as exc:  # noqa: BLE001 — one unrevokable object must not cost the rest the pass
            log.warning("drift_repair_object_failed", extra={"object": target.fga_object, "error": str(exc)})
            out.error = str(exc)
            continue
        done.append(target)
    out.revoked = done
    return out


async def repair_drift(settings: MaintenanceSettings, *, report: ReconcileReport, fga_client: Any) -> RepairReport:
    """Revoke the tuples of every object the report found gone.

    Degrades rather than fails, the same contract `rebuild.py` carries: a store that will not accept
    the deletes leaves `error` set and the tick continues, because a reconcile tick that 500s over a
    failed repair is worse than one reporting drift it could not clear — the report is what everything
    else is gated on.
    """
    if not settings.drift_repair_enabled or fga_client is None:
        return RepairReport(enabled=settings.drift_repair_enabled, dry_run=settings.drift_repair_dry_run)
    planned, refused = plan_repair(report)
    out = RepairReport(enabled=True, dry_run=settings.drift_repair_dry_run, refused=refused)
    out.capped = max(0, len(planned) - settings.drift_repair_max_per_tick)
    planned = planned[: settings.drift_repair_max_per_tick]
    if not planned or out.dry_run:
        out.revoked = planned
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
    return out
