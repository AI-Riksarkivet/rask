"""The drift repair pass revokes tuples whose OBJECT no longer exists — and touches nothing else.

[[LH-061]]. Owner ruling 2026-09-19 (`docs/adr/0066-four-rulings-on-the-lakehouse-s-unblocked-rows-owner-2026.md`) overturned the deferral on a write-capable
reconcile: a repair pass over the drift report, dry-run by DEFAULT with deletion opt-in, alongside the
additive `rebuild.py`. The additive half shipped then; this is the half that can DELETE.

WHAT IT MAY TOUCH, and why the line is exactly there. Three of the report's categories are the same
fact — an FGA object carrying tuples that no registry record names — and revoking those tuples cannot
remove anyone's access to anything, because the object they grant on is gone: `ghost_projects`,
`ghost_warehouses` and `ghost_tables`, each a `GhostObject`. Measured live 2026-09-20: `ghost_tables` alone stands at 1,033, residue of the warehouse cascade
[[LH-148]] fixed at source.

WHAT IT MUST NEVER TOUCH IS THE POINT OF THIS SUITE. `ungoverned_tables` is the inverse shape — a
REAL table that carries no tuples — and a pass that "cleaned up drift" by acting on it would delete
live data to fix an authorization gap. `unbound_namespaces` and `unreferenced_projects` are likewise
real records, and `orphan_buckets`/`orphaned_trash` are bytes. None is in this
increment, and the refusal is asserted per category rather than by a total, because a pass that
planned one data-tier deletion among many revokes would still pass a count.

THE PLANNER LEGS ASSERT ON THE PLAN, because the plan is what a dry run shows an operator before they
arm the thing. THE DRY-RUN, ARMED AND CAP LEGS DRIVE `repair_drift` ITSELF — the coroutine the
reconcile route awaits — with only the two OpenFGA writes stood in: whether a dry run writes is a
property of that coroutine, and a copy of its decision logic could be right while it is wrong.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from maintenance.core.config import MaintenanceSettings
from maintenance.services import repair
from maintenance.services.reconcile import (
    GhostObject,
    OrphanBucket,
    OrphanedTrash,
    ReconcileReport,
    UnboundNamespace,
    UngovernedTable,
    UnreferencedProject,
)
from maintenance.services.repair import RepairReport


def _report() -> ReconcileReport:
    """One finding in every category — so a pass that ignores the split fails here rather than live."""
    return ReconcileReport(
        checked_at="2026-09-20T00:00:00Z",
        ghost_projects=[GhostObject(kind="project", id="gone-p", fga_object="project:gone-p", tuples=3)],
        ghost_warehouses=[GhostObject(kind="warehouse", id="gone-w", fga_object="warehouse:gone-w", tuples=2)],
        ghost_tables=[GhostObject(kind="table", id="ns$gone-t", fga_object="table:ns$gone-t", tuples=4)],
        ungoverned_tables=[UngovernedTable(table="ns$live", root="s3://wh")],
        unbound_namespaces=[UnboundNamespace(namespace="legacy", root="s3://shared")],
        unreferenced_projects=[UnreferencedProject(id="stranded-p")],
        orphan_buckets=[OrphanBucket(bucket="nobodys-bucket")],
        orphaned_trash=[OrphanedTrash(id="tr-1", uuid="b47e2c9a-5f13-4d86-a2c0-8e6f1b3d5a97", kind="table", location="s3://gone/t.lance")],
    )


CATALOG = "http://catalog.test"
_DOOR = f"{CATALOG}/management/v1/grants/ghosts/revoke"


def _settings(**over: object) -> MaintenanceSettings:
    """Built through `model_validate` so the ALIASES are exercised — those strings are the operator's
    interface, and a settings object constructed by field name would pass while the env var an operator
    actually sets did not bind."""
    base: dict[str, object] = {"MAINTENANCE_DRIFT_REPAIR_ENABLED": True, "MAINTENANCE_DRIFT_REPAIR_DRY_RUN": True, "MAINTENANCE_CATALOG_URL": CATALOG}
    return MaintenanceSettings.model_validate({**base, **over})


@pytest.fixture
def door(catalog_identity_token: Path) -> Iterator[respx.MockRouter]:
    """The catalog's side of the wire ([[XC-084]]): maintenance holds no OpenFGA credential, so the revoke is the
    catalog's. A request no route matches fails the test, so a pass that asks when it must not fails here."""
    del catalog_identity_token
    with respx.mock() as router:
        yield router


def _revoking(door: respx.MockRouter, *, kept: str | None = None) -> respx.Route:
    """The ghost door, revoking every object it is asked about but ``kept``, which a registry still names."""

    def _answer(request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.content)["objects"]
        revoked = {obj: 2 for obj in asked if obj != kept}
        return httpx.Response(200, json={"revoked": revoked, "refused": {kept: "a trash record keeps the table for its undrop"} if kept else {}})

    return door.post(_DOOR).mock(side_effect=_answer)


async def _repair(settings: MaintenanceSettings) -> RepairReport:
    return await repair.repair_drift(settings, report=_report())


@pytest.mark.parametrize(
    ("category", "needle"),
    [
        ("ungoverned_tables", "ns$live"),
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


@pytest.mark.asyncio
async def test_a_DRY_RUN_plans_and_asks_NOTHING(door: respx.MockRouter) -> None:
    """The operator arming this must be able to see the plan without it acting: no route, so an ask fails."""
    out = await _repair(_settings())

    assert (out.dry_run, len(out.revoked)) == (True, 3), out.model_dump()


@pytest.mark.asyncio
async def test_DISABLED_does_not_even_plan(door: respx.MockRouter) -> None:
    out = await _repair(_settings(MAINTENANCE_DRIFT_REPAIR_ENABLED=False))

    assert (out.enabled, out.revoked) == (False, [])


@pytest.mark.asyncio
async def test_ARMED_asks_the_catalog_for_exactly_the_planned_objects_and_reports_what_it_kept(door: respx.MockRouter) -> None:
    asked = _revoking(door, kept="table:ns$gone-t")

    out = await _repair(_settings(MAINTENANCE_DRIFT_REPAIR_DRY_RUN=False))

    assert sorted(json.loads(asked.calls.last.request.content)["objects"]) == ["project:gone-p", "table:ns$gone-t", "warehouse:gone-w"]
    assert sorted((r.fga_object, r.tuples) for r in out.revoked) == [("project:gone-p", 2), ("warehouse:gone-w", 2)]
    assert out.kept_by_the_catalog == {"table:ns$gone-t": "a trash record keeps the table for its undrop"}


@pytest.mark.asyncio
async def test_a_door_that_cannot_decide_degrades_rather_than_failing_the_tick(door: respx.MockRouter) -> None:
    """The reconcile report is what everything else is gated on: a repair the catalog could not make is reported."""
    door.post(_DOOR).mock(return_value=httpx.Response(503, json={"code": 17, "detail": "the store did not answer"}))

    out = await _repair(_settings(MAINTENANCE_DRIFT_REPAIR_DRY_RUN=False))

    assert (out.revoked, "could not decide" in (out.error or "")) == ([], True), out.model_dump()


@pytest.mark.asyncio
async def test_the_CAP_truncates_and_SAYS_it_did(door: respx.MockRouter) -> None:
    """A truncated pass that reads as a complete one is how drift looks fixed and is not."""
    asked = _revoking(door)

    out = await _repair(_settings(MAINTENANCE_DRIFT_REPAIR_DRY_RUN=False, MAINTENANCE_DRIFT_REPAIR_MAX_PER_TICK=2))

    assert (len(out.revoked), out.capped, len(json.loads(asked.calls.last.request.content)["objects"])) == (2, 1, 2)


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

    revocable, refused = set(repair._REVOCABLE), set(repair._REFUSED)
    unclassified = sorted(set(CATEGORIES) - (revocable | refused))

    assert unclassified == [], f"{unclassified} are drift categories this pass neither revokes nor refuses by name — classify them in `repair.py`"
    assert not (revocable & refused), "a category cannot be both handled and refused"
