"""The backfill's own decisions: what it skips, what it reports, and what it refuses to guess.

The tuple SET is pinned next door in test_a_new_warehouse_is_granted_to_its_project_and_maintainers.py, shared with the create path so
the two cannot drift. What is tested here is the part only the backfill has — walking a registry that
may contain records it cannot act on, and being honest about it. A repair that half-ran and reported
success is how an estate ends up believing it is repaired.
"""

import pytest

from catalog.api import warehouse_grant_backfill
from catalog.api.authz import warehouse_grants
from lakehouse.governed import fga_provision, fga_resilience
from lakehouse.records.warehouse_records import WarehouseRecord
from tests.catalog_settings import catalog_settings, governed_settings


@pytest.mark.asyncio
async def test_fga_off_is_a_no_op_not_a_failure() -> None:
    """An estate without authorization has nothing to grant — and must not report a failure for it."""
    assert await warehouse_grant_backfill.backfill(catalog_settings()) == (0, 0, [])


@pytest.mark.asyncio
async def test_no_declared_maintainers_is_a_no_op() -> None:
    """Empty by default. An estate that declares no maintenance identity and names no account of its own has no rung to
    grant or take back."""
    assert await warehouse_grant_backfill.backfill(governed_settings()) == (0, 0, [])


@pytest.mark.asyncio
async def test_an_estate_that_declares_only_its_own_accounts_still_runs_the_backfill(monkeypatch) -> None:
    """`backfill` returns early when the release declares nothing. An estate that names its own accounts and no
    maintainer still has rungs to take back: a maintenance account that stopped vending writes keeps `maintenance` on
    every warehouse until a run revokes it, so the guard must not skip that estate.
    """
    settings = governed_settings(LANCE_OWN_ACCOUNTS=["kubernetes~system:serviceaccount:rask:rask-sa-maintenance"])
    skipped: list[str] = []

    def _log_info(event: str, **kw: object) -> None:
        if event == "warehouse_grant_backfill_skipped":
            skipped.append(str(kw.get("extra") or {}))

    monkeypatch.setattr(warehouse_grant_backfill.log, "info", lambda event, **kw: _log_info(event, **kw))

    async def _no_client(*_a: object, **_kw: object) -> None:
        raise RuntimeError("reached the client build, so the guard did not skip")

    monkeypatch.setattr(warehouse_grant_backfill, "build_fga_client", _no_client)

    with pytest.raises(RuntimeError, match="did not skip"):
        await warehouse_grant_backfill.backfill(settings)

    assert skipped == [], f"an estate naming only its own accounts was skipped: {skipped}"


@pytest.mark.asyncio
async def test_one_failure_does_not_abandon_the_rest(monkeypatch) -> None:
    """A single unreachable warehouse must not strand every warehouse after it in the walk."""
    _stub_client(monkeypatch)
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
        governed_settings(LANCE_FGA_MAINTAINERS=["kubernetes~system:serviceaccount:rask:rask-sa-maintenance"])
    )
    assert calls == ["a", "boom", "z"], "the walk stopped at the failure"
    assert (seen, written) == (3, 4)
    assert len(failures) == 1 and "openfga said no" in failures[0]


def _stub_client(monkeypatch) -> None:
    """Replace the OpenFGA client construction — these tests are about the WALK, not the transport.

    Patched on `lakehouse.governed.fga_provision` rather than on this module: the backfill builds its client
    through the estate's ONE bootstrap (`auth_lifespan.build_fga_client`) instead of its own copy of
    pinned-else-provision, so the transport seam is the shared module's, not a name re-exported here.
    """

    class _Client:
        async def close(self) -> None: ...

    monkeypatch.setattr(fga_provision, "make_client", lambda *a, **k: _Client())
    # BOTH HALVES, because which one the bootstrap takes is a POSTURE and postures change:
    # `build_fga_client` defaults to `ModelRole.READER`, so an unpinned build resolves rather
    # than publishes (only the catalog's own lifespan opts in; the default is pinned by
    # `test_attach_auth_postures.py::test_the_default_posture_resolves_when_unpinned`). Stubbing only the
    # write half left the read half dialling a real `openfga:8080`, which fails as a DNS error rather
    # than as anything about the walk these tests are for.
    monkeypatch.setattr(fga_provision, "provision", _provision)
    monkeypatch.setattr(fga_provision, "resolve", _resolve)


async def _provision(
    _url: str,
    *,
    token_file: str | None,
    store_name: str = "lance-catalog",
    store_id: str | None = None,
    retry: fga_resilience.RetryPolicy = fga_resilience.DEFAULT_RETRY,
) -> tuple[str, str]:
    """`fga_provision.provision`'s whole signature, as `_resolve` carries `fga_provision.resolve`'s."""
    del token_file, store_name, retry
    return (store_id or "store", "model")


async def _resolve(
    _url: str,
    *,
    token_file: str | None,
    store_name: str = "lance-catalog",
    store_id: str | None = None,
    deadline_seconds: float = 0.0,
    poll_seconds: float = 0.0,
) -> tuple[str, str]:
    """`fga_provision.resolve`'s whole signature, so a new keyword the caller passes reaches the double."""
    del token_file, store_name, deadline_seconds, poll_seconds
    return (store_id or "store", "model")
