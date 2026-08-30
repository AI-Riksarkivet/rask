"""The backfill's own decisions: what it skips, what it reports, and what it refuses to guess.

The tuple SET is pinned next door in test_cascade_writers_seeded.py, shared with the create path so
the two cannot drift. What is tested here is the part only the backfill has — walking a registry that
may contain records it cannot act on, and being honest about it. A repair that half-ran and reported
success is how an estate ends up believing it is repaired.
"""

from __future__ import annotations

import pytest

from catalog.core.config import Settings
from catalog.services import cascade_backfill


def _settings(**overrides: object) -> Settings:
    base = {"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_S3_SECRET_ACCESS_KEY": "y"}
    return Settings.model_validate({**base, **overrides})


def _fga_settings(**overrides: object) -> Settings:
    return _settings(
        RASK_FGA_ENABLED=True,
        RASK_OIDC_ENABLED=True,
        RASK_OIDC_ISSUER="https://dex",
        RASK_OIDC_AUDIENCE="rask",
        **overrides,
    )


@pytest.mark.asyncio
async def test_fga_off_is_a_no_op_not_a_failure() -> None:
    """An estate without authorization has nothing to grant — and must not report a failure for it."""
    assert await cascade_backfill.backfill(_settings()) == (0, 0, [])


@pytest.mark.asyncio
async def test_no_declared_writers_is_a_no_op() -> None:
    """Empty by default. Merging the backfill cannot change an estate that declares no cascade."""
    assert await cascade_backfill.backfill(_fga_settings()) == (0, 0, [])


@pytest.mark.asyncio
async def test_a_record_without_a_project_is_REPORTED_not_guessed(monkeypatch) -> None:
    """The project edge is what makes the concentric cascade resolve.

    Guessing it — from the warehouse id, say — would grant one tenant's warehouse to another tenant.
    Leaving it unreachable is strictly better, and saying so is what makes it fixable.
    """
    _stub_client(monkeypatch)

    async def _grant(_c, _s, *, warehouse_id: str, project: str, actor: str) -> int:
        return 2

    monkeypatch.setattr(cascade_backfill.fga_deps, "backfill_cascade_grants", _grant)
    monkeypatch.setattr(
        cascade_backfill.warehouses,
        "list_warehouses",
        lambda *a, **k: [{"id": "orphan"}, {"id": "wh1", "project": "acme"}],
    )
    seen, written, failures = await cascade_backfill.backfill(_fga_settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold"]))
    assert seen == 2
    assert written == 2, "the healthy warehouse must still be repaired"
    assert len(failures) == 1 and "orphan" in failures[0] and "no project" in failures[0]


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

    monkeypatch.setattr(cascade_backfill.fga_deps, "backfill_cascade_grants", _flaky)
    monkeypatch.setattr(
        cascade_backfill.warehouses,
        "list_warehouses",
        lambda *a, **k: [{"id": "a", "project": "p"}, {"id": "boom", "project": "p"}, {"id": "z", "project": "p"}],
    )
    seen, written, failures = await cascade_backfill.backfill(_fga_settings(LANCE_FGA_CASCADE_WRITERS=["user:service-silver-to-gold"]))
    assert calls == ["a", "boom", "z"], "the walk stopped at the failure"
    assert (seen, written) == (3, 4)
    assert len(failures) == 1 and "openfga said no" in failures[0]


def test_main_exits_NONZERO_when_a_warehouse_failed(monkeypatch) -> None:
    """A hook that half-ran and exited 0 is indistinguishable from one with nothing to do."""

    async def _one_failure(_settings):
        return (1, 0, ["wh1: openfga said no"])

    monkeypatch.setattr(cascade_backfill, "backfill", _one_failure)
    monkeypatch.setattr(cascade_backfill, "get_settings", lambda: _fga_settings())
    assert cascade_backfill.main() == 1


def test_main_exits_zero_on_a_clean_run(monkeypatch) -> None:
    async def _clean(_settings):
        return (4, 20, [])

    monkeypatch.setattr(cascade_backfill, "backfill", _clean)
    monkeypatch.setattr(cascade_backfill, "get_settings", lambda: _fga_settings())
    assert cascade_backfill.main() == 0


def _stub_client(monkeypatch) -> None:
    """Replace the OpenFGA client construction — these tests are about the WALK, not the transport.

    Patched on `service_kit.governed.fga` rather than on this module: the backfill builds its client
    through the estate's ONE bootstrap (`auth_lifespan.build_fga_client`) instead of its own copy of
    pinned-else-provision, so the transport seam is the shared module's, not a name re-exported here.
    """

    class _Client:
        async def close(self) -> None: ...

    from service_kit.governed import fga

    monkeypatch.setattr(fga, "make_client", lambda *a, **k: _Client())
    monkeypatch.setattr(fga, "provision", _provision)


async def _provision(_url: str) -> tuple[str, str]:
    return ("store", "model")
