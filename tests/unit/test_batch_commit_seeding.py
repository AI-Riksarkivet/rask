"""diff2 F3.1 — a partially-seeded batch must NAME what it stranded, not abandon the rest.

`batch_commit_tables` commits every declared table ATOMICALLY at the native layer, then seeds each
one's FGA ownership in a loop. The seeds cannot join that transaction — OpenFGA is a different store
— so a blip partway through leaves committed tables without an `owner` grant or a `parent` edge. And
because `grant_on_create` writes both in one FGA batch, `owner from parent` cannot rescue them: they
are invisible to every list (per-item filtering) and undroppable by every caller, estate admin
included.

The loop used to stop at the FIRST failure, which maximised the damage (every later table stranded
too) and named none of it. These tests pin the two properties that need no convergence ruling.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import BatchCommitTablesRequest, ServiceUnavailableError

from catalog.api.v1.endpoints import versions as ver
from catalog.core.config import Settings
from service_kit.governed.oidc import IDToken


def _body(names: list[str]) -> BatchCommitTablesRequest:
    ops = [SimpleNamespace(declare_table=SimpleNamespace(id=["db1", n])) for n in names]
    return cast(BatchCommitTablesRequest, SimpleNamespace(operations=ops))


def _drive(monkeypatch: pytest.MonkeyPatch, names: list[str], fail_on: set[str]) -> tuple[list[str], Exception | None]:
    """Run the handler; return (tables successfully seeded, the raised error or None)."""
    seeded: list[str] = []

    async def fake_seed(_client: object, _settings: object, _token: object, *, resource: str, segments: list[str]) -> None:
        name = segments[-1]
        if name in fail_on:
            raise ServiceUnavailableError(f"fga down for {name}")
        seeded.append(name)

    monkeypatch.setattr(ver.fga_deps, "seed_ownership", fake_seed)
    monkeypatch.setattr(ver.native, "call", lambda *_a, **_kw: SimpleNamespace())

    async def no_bound(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(ver, "assert_no_warehouse_bound_namespace", no_bound)

    request = cast(Any, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object()))))
    settings = cast(Settings, SimpleNamespace(delimiter="$", fga_enabled=True))
    token = cast(IDToken, SimpleNamespace(sub="alice"))
    err: Exception | None = None
    try:
        asyncio.run(ver.batch_commit_tables(request, _body(names), cast(Any, object()), settings, token, cast(Any, object())))
    except Exception as exc:  # noqa: BLE001
        err = exc
    return seeded, err


def test_a_mid_batch_seed_failure_still_seeds_every_other_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE ACCEPTANCE CRITERION. Stopping at the first failure strands every table after it too."""
    names = [f"t{i}" for i in range(1, 13)]
    seeded, err = _drive(monkeypatch, names, fail_on={"t5"})

    assert err is not None, "a stranded table must not be reported as success"
    assert seeded == [n for n in names if n != "t5"], (
        f"the loop abandoned the batch at the first failure — only {seeded} were seeded, so 7 more tables were stranded for no reason"
    )


def test_the_error_NAMES_every_stranded_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator repairs these by hand, so 'which ones' is the whole actionable content."""
    _, err = _drive(monkeypatch, ["a", "b", "c"], fail_on={"a", "c"})

    assert err is not None
    message = str(err)
    assert "db1$a" in message and "db1$c" in message, f"stranded tables not named: {message}"
    assert "db1$b" not in message, f"a successfully-seeded table was reported as stranded: {message}"
    # The retry is NOT a repair — saying so is what stops an operator burning the window on it.
    assert "not repair" in message


def test_a_fully_successful_batch_raises_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seeded, err = _drive(monkeypatch, ["a", "b"], fail_on=set())
    assert err is None
    assert seeded == ["a", "b"]
