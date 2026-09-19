"""A tenant that lost its admin tuple can be made administrable again from its own record ([[LH-061]]).

Owner ruling 2026-09-19 (`docs/DECISIONS.md`) overturning the standing "No — not yet" deferral on a
write-capable reconcile: an ADDITIVE tuple rebuild driven from the control-plane registries, dry-run by
default. The resilience property is the point — the estate could DETECT `unreferenced_projects` (a
project record holding no tuples at all) and could do nothing about it, and that state is not cosmetic:
the project-admin tuple is what makes a tenant self-sustaining, so a tenant without it is one nobody can
administer, grant on, or delete.

THESE TESTS ALSO PIN THE BOUND, which matters more than the capability. Measured against the live
control root 2026-09-19: project records carry `created_by` (93 of 93) and warehouse records do NOT
(97 of 97). So the creator's `owner` grant on a warehouse is unrecoverable — the registry never
recorded who it belonged to — and a rebuild that invented one would be granting, not restoring. Only
the tenancy pointer is justified there. `test_a_warehouse_owner_is_NOT_invented` holds that shut.
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


def test_a_tenant_with_no_tuples_gets_its_admin_seed_back() -> None:
    planned, _ = plan_rebuild(_report("acme"), _sources(projects=[_ACME]))

    assert [(t.user, t.relation, t.object) for t in planned] == [("user:CiQwOGE4Njg0Yi", "admin", "project:acme")]


def test_the_seed_is_the_one_the_create_door_writes() -> None:
    """`user:<sub> admin project:<id>`, matching `fga_deps.seed_project_admin` byte for byte.

    A rebuild that invented a different shape would restore a tenant the create door could not have
    produced, and the difference would surface later as an authorization decision nobody can explain.
    """
    planned, _ = plan_rebuild(_report("acme"), _sources(projects=[_ACME]))

    assert planned[0].user.startswith("user:") and planned[0].relation == "admin" and planned[0].object.startswith("project:")


def test_a_healthy_tenant_is_left_alone() -> None:
    """The control. Scoped to the report's findings, not to every record — `write_tuples` is idempotent,
    so re-asserting all 93 seeds per tick would be harmless and unreadable, and the one that mattered
    would be indistinguishable from the noise."""
    assert plan_rebuild(_report(), _sources(projects=[_ACME])) == ([], {})


def test_a_record_with_no_creator_justifies_NOTHING_and_says_so() -> None:
    """The shape that matters: the record exists, the tenant is unadministrable, and this cannot fix it.
    Reported rather than skipped, because a silent skip makes an unrecoverable tenant look recovered."""
    planned, unjustified = plan_rebuild(_report("orphaned"), _sources(projects=[{"id": "orphaned", "created_at": "x"}]))

    assert planned == []
    assert "created_by" in unjustified["project:orphaned"]


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
    """THE BOUND. Warehouse records carry no `created_by` (97 of 97, live 2026-09-19), so there is no
    creator to restore — and a rebuild that picked one would be GRANTING, not restoring."""
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

    assert [t.justified_by for t in planned] == ["_projects/acme.json", "_warehouses/acme-bucket.json"]


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


async def _run(settings: Any, fga_client: Any = _WIRED, **kwargs: Any) -> RebuildReport:
    return await rebuild_tuples(settings, report=_report("acme"), sources=_sources(projects=[_ACME], warehouses=[_ACME_WH]), fga_client=fga_client, **kwargs)


@pytest.mark.asyncio
async def test_a_disabled_pass_writes_nothing(fga: _Fga) -> None:
    report = await _run(_settings(enabled=False))

    assert report.enabled is False
    assert fga.writes == []


@pytest.mark.asyncio
async def test_a_dry_run_reports_the_plan_and_writes_nothing(fga: _Fga) -> None:
    report = await _run(_settings(dry_run=True))

    assert len(report.written) == 2, "a dry run must still say what it would write"
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
    report = await _run(_settings(max_per_tick=1))

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
