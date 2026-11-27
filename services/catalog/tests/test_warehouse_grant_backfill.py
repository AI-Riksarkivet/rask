"""The backfill's own decisions: what it skips, what it reports, and what it refuses to guess.

The tuple SET is pinned next door in test_a_new_warehouse_is_granted_to_its_project_and_maintainers.py, shared with the create path so
the two cannot drift. What is tested here is the part only the backfill has — walking a registry that
may contain records it cannot act on, and being honest about it. A repair that half-ran and reported
success is how an estate ends up believing it is repaired.
"""

import pytest
from openfga_sdk import OpenFgaClient

from catalog.api import warehouse_grant_backfill
from catalog.api.authz import warehouse_grants
from lakehouse.governed import fga_provision
from lakehouse.process.own_threads import ThreadSet
from lakehouse.records.warehouse_records import WarehouseRecord
from tests.catalog_settings import catalog_settings, governed_settings


@pytest.mark.asyncio
async def test_fga_off_is_a_no_op_not_a_failure(own_threads: ThreadSet) -> None:
    """An estate without authorization has nothing to grant — and must not report a failure for it."""
    assert await warehouse_grant_backfill.backfill(catalog_settings(), None, threads=own_threads) == (0, 0, [])


@pytest.mark.asyncio
async def test_no_declared_maintainers_is_a_no_op(own_threads: ThreadSet) -> None:
    """Empty by default. An estate that declares no maintenance identity and names no account of its own has no rung to
    grant or take back."""
    assert await warehouse_grant_backfill.backfill(governed_settings(), _client(), threads=own_threads) == (0, 0, [])


@pytest.mark.asyncio
async def test_an_estate_that_declares_only_its_own_accounts_still_runs_the_backfill(monkeypatch, own_threads: ThreadSet) -> None:
    """`backfill` returns early when the release declares nothing. An estate that names its own accounts and no
    maintainer still has rungs to take back: a maintenance account that stopped vending writes keeps `maintenance` on
    every warehouse until a run revokes it, so the guard must not skip that estate.
    """
    settings = governed_settings(LANCE_OWN_ACCOUNTS=["kubernetes~system:serviceaccount:rask:rask-sa-maintenance"])
    walked: list[str] = []

    def _registry(root: str, _storage_options: object) -> list[WarehouseRecord]:
        walked.append(root)
        return []

    monkeypatch.setattr(warehouse_grant_backfill.warehouses, "list_warehouses", _registry)

    assert (await warehouse_grant_backfill.backfill(settings, _client(), threads=own_threads), walked) == ((0, 0, []), [settings.registry_root])


@pytest.mark.asyncio
async def test_one_failure_does_not_abandon_the_rest(monkeypatch, own_threads: ThreadSet) -> None:
    """A single unreachable warehouse must not strand every warehouse after it in the walk."""
    calls: list[str] = []

    async def _flaky(_c, _s, *, warehouse_id: str, project: str, actor: str) -> int:
        calls.append(warehouse_id)
        if warehouse_id == "boom":
            raise RuntimeError("openfga said no")
        return 2

    monkeypatch.setattr(warehouse_grants, "converge_warehouse_grants", _flaky)
    monkeypatch.setattr(
        warehouse_grant_backfill.warehouses,
        "list_warehouses",
        lambda *a, **k: [WarehouseRecord(id=name, bucket=f"b-{name}", project="p", root_uri=f"s3://b-{name}") for name in ("a", "boom", "z")],
    )
    seen, written, failures = await warehouse_grant_backfill.backfill(
        governed_settings(LANCE_FGA_MAINTAINERS=["kubernetes~system:serviceaccount:rask:rask-sa-maintenance"]), _client(), threads=own_threads
    )
    assert calls == ["a", "boom", "z"], "the walk stopped at the failure"
    assert (seen, written) == (3, 4)
    assert len(failures) == 1 and "openfga said no" in failures[0]


def _client() -> OpenFgaClient:
    """The client the lifespan wires, never dialled: these tests are about the WALK, and the convergence is stubbed."""
    return fga_provision.make_client("http://openfga.test:8080", "store", "model", token_file=None)
