"""A tenant that lost its admin tuple can be made administrable again from its own record ([[LH-061]]).

Owner ruling 2026-09-19 (`docs/adr/0066-four-rulings-on-the-lakehouse-s-unblocked-rows-owner-2026.md`) overturning the standing "No — not yet" deferral on a
write-capable reconcile: an ADDITIVE tuple rebuild driven from the control-plane registries, dry-run by
default. The resilience property is the point — the estate could DETECT `unreferenced_projects` (a
project record holding no tuples at all) and could do nothing about it, and that state is not cosmetic:
the project-admin tuple is what makes a tenant self-sustaining, so a tenant without it is one nobody can
administer, grant on, or delete.

THE LINE THESE TESTS HOLD is narrower than the row asked for: the rebuild writes the parent/child edges between
the estate, projects, warehouses, namespaces, tables and roles, and leaves ownership tuples, grants, role
assignments, bootstrap admin tuples and the model's own bookkeeping alone.

It is right for a specific reason: **the registry does not record REVOCATION.** Project records carry `created_by`
(93 of 93, live 2026-09-19) and warehouse records do not (0 of 97) — but `created_by` names who CREATED a tenant, never who may administer it today. An
admin deliberately removed leaves the record untouched, so re-asserting that grant restores exactly the
person an operator took care to remove. `test_a_stranded_project_is_REPORTED_not_re_granted` is the one
that matters here; a structural edge cannot confer access, and a grant can.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from maintenance.core.config import MaintenanceSettings
from maintenance.services.rebuild import RebuildReport, plan_rebuild, rebuild_tuples
from maintenance.services.reconcile import ReconcileReport, Sources, UnreferencedProject


CATALOG = "http://catalog.test"
_DOOR = f"{CATALOG}/management/v1/grants/tenancy/rebuild"


def _settings(**over: object) -> MaintenanceSettings:
    """Built through `model_validate`, so the aliases an operator sets are the ones exercised."""
    base: dict[str, object] = {"MAINTENANCE_TUPLE_REBUILD_ENABLED": True, "MAINTENANCE_TUPLE_REBUILD_DRY_RUN": False, "MAINTENANCE_CATALOG_URL": CATALOG}
    return MaintenanceSettings.model_validate({**base, **over})


def _report(*unreferenced: str) -> ReconcileReport:
    return ReconcileReport(checked_at="2026-09-19T00:00:00Z", unreferenced_projects=[UnreferencedProject(id=p) for p in unreferenced])


def _sources(*, projects: list[dict[str, str]] | None = None, warehouses: list[dict[str, str]] | None = None) -> Sources:
    return Sources(project_records=projects or [], warehouse_records=warehouses or [])


_ACME = {"id": "acme", "created_at": "2026-08-07T06:48:27.0329", "created_by": "CiQwOGE4Njg0Yi"}
_ACME_WH = {"id": "acme-bucket", "bucket": "acme-bucket", "root_uri": "s3://acme-bucket", "project": "acme", "status": "active"}


def test_a_stranded_project_is_REPORTED_not_re_granted() -> None:
    """THE LINE. A tenant holding no tuples needs an admin grant, and a grant is a decision.

    `created_by` is right there and this deliberately does not use it: the registry records creation
    and never revocation, so re-asserting it re-grants precisely the person an operator may have taken
    care to remove — a privilege restoration wearing a repair's name.
    """
    planned, unjustified = plan_rebuild(_report("acme"), _sources(projects=[_ACME]))

    assert planned == [], "a project admin grant was written by a repair pass"
    assert "decision, not a repair" in unjustified["project:acme"]


def test_no_tuple_this_pass_writes_confers_ACCESS() -> None:
    """The property that makes it safe to run unattended: a structural edge says where an object lives.

    `admin`, `owner`, `writer`, `reader` and the rest are grants; `project` and `parent` are pointers.
    Asserted over everything planned, so a later tier cannot quietly add a granting relation.
    """
    planned, _ = plan_rebuild(_report("acme"), _sources(projects=[_ACME], warehouses=[_ACME_WH]))

    assert {t.relation for t in planned} <= {"project", "parent"}, [t.relation for t in planned]


def test_a_record_with_no_creator_says_SO_rather_than_going_quiet() -> None:
    """Worse than a stranded tenant: one with no recorded creator at all, so not even a candidate exists.
    Reported rather than skipped, because a silent skip makes an unrecoverable tenant look recovered."""
    planned, unjustified = plan_rebuild(_report("orphaned"), _sources(projects=[{"id": "orphaned", "created_at": "x"}]))

    assert planned == []
    assert "no `created_by`" in unjustified["project:orphaned"]


def test_a_warehouse_under_a_revived_tenant_regains_its_tenancy_pointer() -> None:
    planned, _ = plan_rebuild(_report("acme"), _sources(projects=[_ACME], warehouses=[_ACME_WH]))

    assert ("project:acme", "project", "warehouse:acme-bucket") in [(t.user, t.relation, t.object) for t in planned]


def test_a_warehouse_under_a_healthy_tenant_is_left_alone() -> None:
    """The control for the pointer half."""
    planned, _ = plan_rebuild(_report(), _sources(projects=[_ACME], warehouses=[_ACME_WH]))

    assert planned == []


# --- the write path --------------------------------------------------------------------------- #


@pytest.fixture
def door(catalog_identity_token: Path) -> Iterator[respx.MockRouter]:
    """The catalog's side of the wire ([[XC-084]]): maintenance holds no OpenFGA credential, so the write is the
    catalog's. A request no route matches fails the test, so a pass that asks when it must not fails here."""
    del catalog_identity_token
    with respx.mock() as router:
        yield router


def _writing(door: respx.MockRouter) -> respx.Route:
    """The tenancy door, writing each edge a warehouse it is asked about names, and answering the rest unjustified."""

    def _answer(request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.content)["warehouses"]
        return httpx.Response(200, json={"written": [f"project:acme project warehouse:{w}" for w in asked if w != "acme-second"], "unjustified": {}})

    return door.post(_DOOR).mock(side_effect=_answer)


async def _run(settings: MaintenanceSettings, warehouses: list[dict[str, str]] | None = None) -> RebuildReport:
    sources = _sources(projects=[_ACME], warehouses=warehouses or [_ACME_WH])
    return await rebuild_tuples(settings, report=_report("acme"), sources=sources)


@pytest.mark.asyncio
async def test_a_disabled_pass_asks_nothing(door: respx.MockRouter) -> None:
    report = await _run(_settings(MAINTENANCE_TUPLE_REBUILD_ENABLED=False))

    assert (report.enabled, report.written) == (False, [])


@pytest.mark.asyncio
async def test_a_dry_run_reports_the_plan_and_asks_nothing(door: respx.MockRouter) -> None:
    report = await _run(_settings(MAINTENANCE_TUPLE_REBUILD_DRY_RUN=True))

    assert [(t.user, t.relation, t.object) for t in report.written] == [("project:acme", "project", "warehouse:acme-bucket")]


@pytest.mark.asyncio
async def test_the_catalog_writes_the_planned_edges_and_the_pass_reports_what_it_wrote(door: respx.MockRouter) -> None:
    asked = _writing(door)

    report = await _run(_settings())

    assert json.loads(asked.calls.last.request.content) == {"warehouses": ["acme-bucket"]}
    assert [(t.user, t.relation, t.object) for t in report.written] == [("project:acme", "project", "warehouse:acme-bucket")]


@pytest.mark.asyncio
async def test_a_door_that_cannot_decide_degrades_rather_than_failing_the_tick(door: respx.MockRouter) -> None:
    """The reconcile report is what everything else is gated on. A tick that 500s because a repair
    failed is worse than one that reports drift it could not fix."""
    door.post(_DOOR).mock(return_value=httpx.Response(503, json={"code": 17, "detail": "the store did not answer"}))

    report = await _run(_settings())

    assert (report.written, "could not decide" in (report.error or "")) == ([], True), report.model_dump()


@pytest.mark.asyncio
async def test_the_remainder_beyond_the_cap_is_reported(door: respx.MockRouter) -> None:
    """A wholesale tuple loss makes every tenant eligible at once, so an uncapped pass would turn one
    cron fire into an estate-wide authorization write."""
    asked = _writing(door)

    report = await _run(_settings(MAINTENANCE_TUPLE_REBUILD_MAX_PER_TICK=1), warehouses=[_ACME_WH, {"id": "acme-second", "project": "acme"}])

    assert (len(report.written), report.capped, json.loads(asked.calls.last.request.content)) == (1, 1, {"warehouses": ["acme-bucket"]})
