"""The drift repair pass revokes tuples whose OBJECT no longer exists — and touches nothing else.

[[LH-061]]. Owner ruling 2026-09-19 (`docs/DECISIONS.md`) overturned the deferral on a write-capable
reconcile: a repair pass over the drift report, dry-run by DEFAULT with deletion opt-in, alongside the
additive `rebuild.py`. The additive half shipped then; this is the half that can DELETE.

WHAT IT MAY TOUCH, and why the line is exactly there. Four of the report's categories are the same
fact — an FGA object carrying tuples that no registry record names — and revoking those tuples cannot
remove anyone's access to anything, because the object they grant on is gone. `ghost_projects`,
`ghost_warehouses` and `ghost_tables` are `GhostObject`; `orphaned_annotation_tasks` is an edge naming
a retired tenant, which `reconcile.py` already describes as "not dangerous, it is UNREACHABLE".
Measured live 2026-09-20: `ghost_tables` alone stands at 1,033, residue of the warehouse cascade
[[LH-148]] fixed at source.

WHAT IT MUST NEVER TOUCH IS THE POINT OF THIS SUITE. `ungoverned_tables` is the inverse shape — a
REAL table that carries no tuples — and a pass that "cleaned up drift" by acting on it would delete
live data to fix an authorization gap. `unbound_namespaces` and `unreferenced_projects` are likewise
real records, and `orphan_buckets`/`orphan_files`/`orphaned_trash` are bytes. None is in this
increment, and the refusal is asserted per category rather than by a total, because a pass that
planned one data-tier deletion among many revokes would still pass a count.

THE TEST IS ON THE PLAN, not on a mock's call log: the plan is what a dry run shows an operator
before they arm the thing, so it is the artifact that has to be right.
"""

from __future__ import annotations

import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services import repair
from maintenance.services.reconcile import (
    GhostObject,
    OrphanBucket,
    OrphanedAnnotationTask,
    OrphanedTrash,
    ReconcileReport,
    UnboundNamespace,
    UngovernedTable,
    UnreferencedProject,
)


def _report() -> ReconcileReport:
    """One finding in every category — so a pass that ignores the split fails here rather than live."""
    return ReconcileReport(
        checked_at="2026-09-20T00:00:00Z",
        ghost_projects=[GhostObject(kind="project", id="gone-p", fga_object="project:gone-p", tuples=3)],
        ghost_warehouses=[GhostObject(kind="warehouse", id="gone-w", fga_object="warehouse:gone-w", tuples=2)],
        ghost_tables=[GhostObject(kind="table", id="ns$gone-t", fga_object="table:ns$gone-t", tuples=4)],
        orphaned_annotation_tasks=[OrphanedAnnotationTask(annotation_project="anno-1", tenant="gone-p")],
        ungoverned_tables=[UngovernedTable(table="ns$live", root="s3://wh")],
        unbound_namespaces=[UnboundNamespace(namespace="legacy", root="s3://shared")],
        unreferenced_projects=[UnreferencedProject(id="stranded-p")],
        orphan_buckets=[OrphanBucket(bucket="nobodys-bucket")],
        orphaned_trash=[OrphanedTrash(id="tr-1", kind="table", location="s3://gone/t.lance")],
    )


def _settings(**over: object) -> MaintenanceSettings:
    """Built through `model_validate` so the ALIASES are exercised — those strings are the operator's
    interface, and a settings object constructed by field name would pass while the env var an operator
    actually sets did not bind."""
    base: dict[str, object] = {"MAINTENANCE_DRIFT_REPAIR_ENABLED": True, "MAINTENANCE_DRIFT_REPAIR_DRY_RUN": True}
    return MaintenanceSettings.model_validate({**base, **over})


class _Recorder:
    """A revoke that records instead of calling OpenFGA — a class rather than a lambda so its return
    type is the `Sequence` the seam declares, not whatever `list.append` happens to give back."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, obj: str) -> list[object]:
        self.seen.append(obj)
        return []


def test_it_plans_a_revoke_for_every_object_that_is_GONE() -> None:
    planned, _ = repair.plan_repair(_report())

    assert {p.fga_object for p in planned} == {"project:gone-p", "warehouse:gone-w", "table:ns$gone-t"}


def test_every_planned_revoke_NAMES_the_finding_that_justifies_it() -> None:
    """An audit row saying a reconciler revoked four grants is unreadable without the why."""
    planned, _ = repair.plan_repair(_report())

    assert all(p.justified_by for p in planned), [p.model_dump() for p in planned]
    assert {p.justified_by for p in planned} == {"ghost_projects", "ghost_warehouses", "ghost_tables"}


@pytest.mark.parametrize(
    ("category", "needle"),
    [
        ("ungoverned_tables", "ns$live"),
        ("unbound_namespaces", "legacy"),
        ("unreferenced_projects", "stranded-p"),
        ("orphan_buckets", "nobodys-bucket"),
        ("orphaned_trash", "tr-1"),
    ],
)
def test_a_DATA_tier_finding_is_never_planned_for_deletion(category: str, needle: str) -> None:
    """THE DEFECT THIS SUITE EXISTS FOR: `ns$live` is a real table with real bytes and no tuples.

    Per category rather than by a total, because a pass planning one data-tier deletion among a dozen
    legitimate revokes would satisfy any assertion about the count.
    """
    planned, refused = repair.plan_repair(_report())

    assert not any(needle in p.fga_object for p in planned), f"{category} was planned for deletion — that destroys a real object"
    assert category in refused, f"{category} is silently ignored; it must be REFUSED by name so a reader can see the pass considered it"


def test_a_DRY_RUN_plans_and_revokes_NOTHING() -> None:
    """The operator arming this must be able to see the plan without it acting."""
    revoke = _Recorder()
    out = repair.repair_drift_sync(_settings(), report=_report(), revoke=revoke)

    assert out.dry_run is True
    assert len(out.revoked) == 3, out.model_dump()
    assert revoke.seen == [], f"a dry run revoked {revoke.seen}"


def test_DISABLED_does_not_even_plan() -> None:
    revoke = _Recorder()
    out = repair.repair_drift_sync(_settings(MAINTENANCE_DRIFT_REPAIR_ENABLED=False), report=_report(), revoke=revoke)

    assert out.enabled is False
    assert out.revoked == [] and revoke.seen == []


def test_ARMED_revokes_exactly_the_planned_objects() -> None:
    revoke = _Recorder()
    out = repair.repair_drift_sync(_settings(MAINTENANCE_DRIFT_REPAIR_DRY_RUN=False), report=_report(), revoke=revoke)

    assert sorted(revoke.seen) == ["project:gone-p", "table:ns$gone-t", "warehouse:gone-w"]
    assert out.dry_run is False and len(out.revoked) == 3


def test_the_CAP_truncates_and_SAYS_it_did() -> None:
    """A truncated pass that reads as a complete one is how drift looks fixed and is not."""
    out = repair.repair_drift_sync(_settings(MAINTENANCE_DRIFT_REPAIR_MAX_PER_TICK=2), report=_report(), revoke=_Recorder())

    assert len(out.revoked) == 2 and out.capped == 1


def test_a_CLEAN_report_plans_nothing() -> None:
    """The control. A pass that revoked on an empty report would pass every leg above."""
    planned, refused = repair.plan_repair(ReconcileReport(checked_at="2026-09-20T00:00:00Z"))

    assert planned == [] and refused == {}


def test_EVERY_drift_category_is_either_revocable_or_refused_BY_NAME() -> None:
    """The invariant that keeps this safe as the report grows, and the reason the two lists are data.

    A new drift category added to `reconcile.CATEGORIES` must be classified deliberately. Without this
    it defaults into `_REFUSED`'s absence — silently skipped, which reads as safe — or, if someone adds
    it to `_REVOCABLE` for symmetry, into being DELETED. Neither should be reachable by forgetting.
    """
    from maintenance.services.reconcile import CATEGORIES

    revocable, edges, refused = set(repair._REVOCABLE), set(repair._EDGE_ONLY), set(repair._REFUSED)
    unclassified = sorted(set(CATEGORIES) - (revocable | edges | refused))

    assert unclassified == [], f"{unclassified} are drift categories this pass neither revokes, cuts nor refuses by name — classify them in `repair.py`"
    assert not (revocable & edges), "a category cannot be both a whole-object revoke and an exact-edge cut"
    assert not (revocable & refused) and not (edges & refused), "a category cannot be both handled and refused"


def test_the_refusals_name_a_REAL_category() -> None:
    """The backward half: a refusal for a category that no longer exists makes the gate above vacuous."""
    from maintenance.services.reconcile import CATEGORIES

    stale = sorted((set(repair._REVOCABLE) | set(repair._EDGE_ONLY) | set(repair._REFUSED)) - set(CATEGORIES))

    assert stale == [], f"{stale} are classified here and are not drift categories — remove them"


# --------------------------------------------------------------------------- #
# The EDGE half: a live object with one dead edge ([[LH-061]])
# --------------------------------------------------------------------------- #


def test_an_orphaned_annotation_task_plans_an_EDGE_cut_not_an_object_revoke() -> None:
    """The distinction this module turns on, now with a seam for the other side of it.

    `orphaned_annotation_tasks` was refused because the annotation project still EXISTS — only its
    `tenant` edge names a project that does not — and `revoke_object_tuples` is all-or-nothing by
    object, so using it would destroy a live object's whole authorization to clear one stale edge.
    The finding carries BOTH ends, so the exact tuple is fully determined and needs no lookup.
    """
    _planned, edges, _refused = repair.plan_repair_edges(_report())

    assert [(e.user, e.relation, e.object) for e in edges] == [("project:gone-p", "tenant", "annotation_project:anno-1")]


def test_the_annotation_task_is_NOT_planned_as_an_object_revoke() -> None:
    """THE DEFECT: revoking the object would take every OTHER grant on a live annotation project."""
    planned, _edges, _refused = repair.plan_repair_edges(_report())

    assert not any("annotation_project" in p.fga_object for p in planned), "a live object was planned for a whole-object revoke"


def test_the_annotation_category_has_LEFT_the_refusal_list() -> None:
    """A category that is now handled must not still be reported as declined — that is the backward
    half `test_every_recorded_violation_is_still_real` applies to the sibling ratchet, for the same
    reason: a stale refusal makes the remaining count unreadable."""
    _planned, _edges, refused = repair.plan_repair_edges(_report())

    assert "orphaned_annotation_tasks" not in refused
    assert "orphaned_annotation_tasks" not in repair._REFUSED


def test_a_DRY_RUN_cuts_no_edge() -> None:
    cut: list[str] = []
    out = repair.repair_drift_sync(_settings(), report=_report(), revoke=_Recorder(), cut_edge=lambda e: cut.append(e.object))

    assert out.dry_run is True
    assert len(out.edges_cut) == 1, out.model_dump()
    assert cut == [], f"a dry run cut {cut}"


def test_ARMED_cuts_exactly_the_dangling_edge() -> None:
    cut: list[str] = []
    out = repair.repair_drift_sync(
        _settings(MAINTENANCE_DRIFT_REPAIR_DRY_RUN=False), report=_report(), revoke=_Recorder(), cut_edge=lambda e: cut.append(e.object)
    )

    assert cut == ["annotation_project:anno-1"]
    assert len(out.edges_cut) == 1


def test_every_EDGE_ONLY_category_is_actually_built_into_a_plan() -> None:
    """`_EDGE_ONLY` exists for the coverage invariant, so a name listed there that the planner ignores
    would satisfy the gate while cutting nothing — the vacuous shape this estate keeps finding."""
    report = _report()
    _planned, edges, _refused = repair.plan_repair_edges(report)

    built = {e.justified_by for e in edges}
    assert built == set(repair._EDGE_ONLY), f"declared {sorted(repair._EDGE_ONLY)} but planned {sorted(built)}"
