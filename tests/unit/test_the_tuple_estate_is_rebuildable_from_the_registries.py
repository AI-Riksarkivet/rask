"""A tenant that lost its admin tuple can be made administrable again from its own record ([[LH-061]]).

Owner ruling 2026-09-19 (`docs/DECISIONS.md`) overturning the standing "No — not yet" deferral on a
write-capable reconcile: an ADDITIVE tuple rebuild driven from the control-plane registries, dry-run by
default. The resilience property is the point — the estate could DETECT `unreferenced_projects` (a
project record holding no tuples at all) and could do nothing about it, and that state is not cosmetic:
the project-admin tuple is what makes a tenant self-sustaining, so a tenant without it is one nobody can
administer, grant on, or delete.

THE LINE THESE TESTS HOLD IS UPSTREAM'S, and it is narrower than the row asked for. Lakekeeper's
`lakekeeper openfga reconcile` rebuilds "the parent/child edges between server, projects, warehouses,
namespaces, tables, views, and roles" and leaves "ownership tuples, grants, role assignments, bootstrap
admin tuples, and authorization-model bookkeeping ... alone"
(https://docs.lakekeeper.io/docs/latest/authorization-openfga/).

It is right for rask for a specific reason, not by deference: **the registry does not record
REVOCATION.** Project records carry `created_by` (93 of 93, live 2026-09-19) and warehouse records do
not (0 of 97) — but `created_by` names who CREATED a tenant, never who may administer it today. An
admin deliberately removed leaves the record untouched, so re-asserting that grant restores exactly the
person an operator took care to remove. `test_a_stranded_project_is_REPORTED_not_re_granted` is the one
that matters here; a structural edge cannot confer access, and a grant can.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from maintenance.services.rebuild import RebuildReport, plan_rebuild, rebuild_tuples
from maintenance.services.reconcile import ReconcileReport, Sources, UnreferencedProject


class _Settings:
    def __init__(self, *, enabled: bool = True, dry_run: bool = False, max_per_tick: int = 50) -> None:
        self.tuple_rebuild_enabled = enabled
        self.tuple_rebuild_dry_run = dry_run
        self.tuple_rebuild_max_per_tick = max_per_tick
        self.catalog_service_identity = "service-maintenance"


def _settings(**kwargs: Any) -> Any:
    return cast(Any, _Settings(**kwargs))


def _report(*unreferenced: str) -> ReconcileReport:
    return ReconcileReport(checked_at="2026-09-19T00:00:00Z", unreferenced_projects=[UnreferencedProject(id=p) for p in unreferenced])


def _sources(*, projects: list[dict[str, str]] | None = None, warehouses: list[dict[str, str]] | None = None) -> Sources:
    return Sources(project_records=projects or [], warehouse_records=warehouses or [])


_ACME = {"id": "acme", "created_at": "2026-08-07T06:48:27.0329", "created_by": "CiQwOGE4Njg0Yi", "protected": "false"}
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


def test_the_finding_names_the_candidate_so_the_decision_is_one_call_away() -> None:
    """Refusing to grant must not mean refusing to help — the operator needs the id the record holds."""
    _, unjustified = plan_rebuild(_report("acme"), _sources(projects=[_ACME]))

    assert "CiQwOGE4Njg0Yi" in unjustified["project:acme"]


def test_no_tuple_this_pass_writes_confers_ACCESS() -> None:
    """The property that makes it safe to run unattended: a structural edge says where an object lives.

    `admin`, `owner`, `writer`, `reader` and the rest are grants; `project` and `parent` are pointers.
    Asserted over everything planned, so a later tier cannot quietly add a granting relation.
    """
    planned, _ = plan_rebuild(_report("acme"), _sources(projects=[_ACME], warehouses=[_ACME_WH]))

    assert {t.relation for t in planned} <= {"project", "parent"}, [t.relation for t in planned]


def test_a_healthy_tenant_is_left_alone() -> None:
    """The control. Scoped to the report's findings, not to every record — `write_tuples` is idempotent,
    so re-asserting all 93 seeds per tick would be harmless and unreadable, and the one that mattered
    would be indistinguishable from the noise."""
    assert plan_rebuild(_report(), _sources(projects=[_ACME])) == ([], {})


def test_a_record_with_no_creator_says_SO_rather_than_going_quiet() -> None:
    """Worse than a stranded tenant: one with no recorded creator at all, so not even a candidate exists.
    Reported rather than skipped, because a silent skip makes an unrecoverable tenant look recovered."""
    planned, unjustified = plan_rebuild(_report("orphaned"), _sources(projects=[{"id": "orphaned", "created_at": "x"}]))

    assert planned == []
    assert "no `created_by`" in unjustified["project:orphaned"]


def test_a_warehouse_under_a_revived_tenant_regains_its_tenancy_pointer() -> None:
    planned, _ = plan_rebuild(_report("acme"), _sources(projects=[_ACME], warehouses=[_ACME_WH]))

    assert ("project:acme", "project", "warehouse:acme-bucket") in [(t.user, t.relation, t.object) for t in planned]


def test_the_pointer_relation_is_project_NOT_parent() -> None:
    """`model.fga` defines `project: [project]` on the warehouse type and declares no `parent` there.
    Writing `parent` makes OpenFGA reject the whole seed with a 503, which is why the catalog's
    `seed_warehouse` writes this relation directly rather than through `grant_on_create`."""
    planned, _ = plan_rebuild(_report("acme"), _sources(projects=[_ACME], warehouses=[_ACME_WH]))

    assert [t.relation for t in planned if t.object.startswith("warehouse:")] == ["project"]


def test_a_warehouse_owner_is_NOT_invented() -> None:
    """Warehouse records carry no `created_by` (0 of 97, live 2026-09-19), so there is no creator to
    restore — and a rebuild that picked one would be GRANTING, not restoring. The same rule as the
    project seed above, arriving by a second route: here the field is absent, there it is present and
    still not a licence."""
    planned, _ = plan_rebuild(_report("acme"), _sources(projects=[_ACME], warehouses=[_ACME_WH]))

    assert [t for t in planned if t.relation == "owner"] == []


def test_a_warehouse_under_a_healthy_tenant_is_left_alone() -> None:
    """The control for the pointer half."""
    planned, _ = plan_rebuild(_report(), _sources(projects=[_ACME], warehouses=[_ACME_WH]))

    assert planned == []


def test_every_planned_tuple_names_the_record_that_justifies_it() -> None:
    """A reader must be able to check the claim without this module — that is what makes it a REBUILD
    rather than a grant."""
    planned, _ = plan_rebuild(_report("acme"), _sources(projects=[_ACME], warehouses=[_ACME_WH]))

    assert [t.justified_by for t in planned] == ["_warehouses/acme-bucket.json"]


# --- the write path --------------------------------------------------------------------------- #


class _Fga:
    def __init__(self, *, fail: bool = False) -> None:
        self.writes: list[Any] = []
        self.origins: list[str] = []
        self.fail = fail

    async def write_tuples(self, client: Any, tuples: list[Any], *, actor: str, origin: str) -> None:
        if self.fail:
            raise RuntimeError("store unreachable")
        self.writes.extend(tuples)
        self.origins.append(origin)


@pytest.fixture
def fga(monkeypatch: pytest.MonkeyPatch) -> _Fga:
    spy = _Fga()
    monkeypatch.setattr("maintenance.services.rebuild.fga.write_tuples", spy.write_tuples)
    return spy


#: A stand-in for a wired OpenFGA client. Only its NON-NONE-ness is read here — the write itself is
#: spied at `fga.write_tuples`, which is the seam the module actually calls.
_WIRED = object()


async def _run(settings: Any, fga_client: Any = _WIRED, warehouses: list[dict[str, str]] | None = None) -> RebuildReport:
    sources = _sources(projects=[_ACME], warehouses=warehouses or [_ACME_WH])
    return await rebuild_tuples(settings, report=_report("acme"), sources=sources, fga_client=fga_client)


@pytest.mark.asyncio
async def test_a_disabled_pass_writes_nothing(fga: _Fga) -> None:
    report = await _run(_settings(enabled=False))

    assert report.enabled is False
    assert fga.writes == []


@pytest.mark.asyncio
async def test_a_dry_run_reports_the_plan_and_writes_nothing(fga: _Fga) -> None:
    report = await _run(_settings(dry_run=True))

    assert len(report.written) == 1, "a dry run must still say what it would write"
    assert fga.writes == []


@pytest.mark.asyncio
async def test_the_write_carries_its_own_origin(fga: _Fga) -> None:
    """`registry_rebuild`, not `project_create`. An audit row claiming a tuple was minted with its
    project — when it was reconstructed from a record months later — destroys the one property the
    origin field exists for, which is the reason `cascade_backfill` is also its own value."""
    await _run(_settings())

    assert fga.origins == ["registry_rebuild"]


@pytest.mark.asyncio
async def test_an_unreachable_store_degrades_rather_than_failing_the_tick() -> None:
    """The reconcile report is what everything else is gated on. A tick that 500s because a repair
    failed is worse than one that reports drift it could not fix."""
    spy = _Fga(fail=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("maintenance.services.rebuild.fga.write_tuples", spy.write_tuples)
        report = await _run(_settings())

    assert report.error == "store unreachable"
    assert report.written == []


@pytest.mark.asyncio
async def test_no_fga_client_writes_nothing(fga: _Fga) -> None:
    report = await _run(_settings(), fga_client=None)

    assert report.written == [] and fga.writes == []


@pytest.mark.asyncio
async def test_the_remainder_beyond_the_cap_is_reported(fga: _Fga) -> None:
    """A wholesale tuple loss makes every tenant eligible at once, so an uncapped pass would turn one
    cron fire into an estate-wide authorization write."""
    report = await _run(_settings(max_per_tick=1), warehouses=[_ACME_WH, {"id": "acme-second", "project": "acme"}])

    assert len(report.written) == 1
    assert report.capped == 1


@pytest.mark.asyncio
async def test_the_pass_never_deletes(fga: _Fga) -> None:
    """The property the whole design rests on: it cannot widen access beyond what the control plane
    already recorded, because it only ever adds what a record justifies."""
    import ast
    import inspect

    import maintenance.services.rebuild as module

    source = inspect.getsource(module)
    called = {node.func.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}

    assert not (called & {"delete_tuples", "revoke", "delete", "write_deletes"}), f"a deleting call appeared in the additive rebuild: {called}"
