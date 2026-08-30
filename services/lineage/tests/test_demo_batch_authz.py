"""The demo peek authorizes with ONE batch_check, and the batch payload carries no duplicates (F-LIN-13).

The `/demo/datasets` gate looped ``require_metadata_access`` per dataset — one OpenFGA round trip per
``_LAYOUT`` entry — while the module's own ``DatasetFilter`` already batch-checks. And
``DatasetFilter.visible`` forwarded its ``names`` list verbatim, so a caller passing per-column dataset
names (columns.py's subgraph view) sent a duplicate-laden batch payload.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from lineage.api import fga_deps
from lineage.api.fga_deps import DatasetFilter
from lineage.api.security import authenticate
from lineage.api.v1.endpoints import demo
from lineage.core.config import LineageSettings, get_settings
from lineage.schemas import DemoDataset


def _settings() -> LineageSettings:
    # FGA refuses to enable without OIDC (fail-closed config) — construct the real pair, no flag patching.
    return LineageSettings(
        fga_enabled=True,
        oidc_enabled=True,
        oidc_issuer="https://example.invalid/realms/rask",
        oidc_audience="rask",
    )


class _Principal:
    sub = "demo-viewer"


class _Spy:
    """Counts single vs batch OpenFGA calls and records every batch payload."""

    def __init__(self) -> None:
        self.single = 0
        self.batches: list[list[str]] = []

    async def check(self, _client: object, *, user: str, relation: str, obj: str) -> bool:
        self.single += 1
        return True

    async def batch_check(self, _client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        self.batches.append(list(objects))
        return dict.fromkeys(objects, True)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    spy = _Spy()
    monkeypatch.setattr(fga_deps.fga, "check", spy.check)
    monkeypatch.setattr(fga_deps.fga, "batch_check", spy.batch_check)
    return spy


def test_demo_authorizes_with_one_batch_check(monkeypatch: pytest.MonkeyPatch, spy: _Spy) -> None:
    """N datasets, ONE FGA round trip — the loop of single checks is the defect."""

    def _stub_read(*_a: object, **_k: object) -> DemoDataset:
        return DemoDataset(name="stub", uri="stub", exists=False)

    monkeypatch.setattr(demo, "_read_dataset", _stub_read)
    app = FastAPI()
    app.include_router(demo.router)
    app.state.fga = object()
    settings = _settings()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[authenticate] = lambda: _Principal()

    response = TestClient(app).get("/demo/datasets")

    assert response.status_code == 200
    assert spy.single == 0, f"{spy.single} sequential single check() calls — the gate must batch"
    assert len(spy.batches) == 1, f"expected exactly one batch_check, got {len(spy.batches)}"
    assert sorted(spy.batches[0]) == sorted(f"table:{name}" for name, _ in demo._LAYOUT)


def test_demo_denied_dataset_is_dropped_not_fatal(monkeypatch: pytest.MonkeyPatch, spy: _Spy) -> None:
    """A 403 on one dataset skips it (the loop's `continue` semantics survive the batching)."""

    async def _deny_bronze(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        spy.batches.append(list(objects))
        return {o: ("bronze" not in o) for o in objects}

    monkeypatch.setattr(fga_deps.fga, "batch_check", _deny_bronze)

    def _stub_read(*args: object, **_k: object) -> DemoDataset:
        name = next(a for a in args if isinstance(a, str))
        return DemoDataset(name=name, uri="stub", exists=False)

    monkeypatch.setattr(demo, "_read_dataset", _stub_read)
    app = FastAPI()
    app.include_router(demo.router)
    app.state.fga = object()
    settings = _settings()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[authenticate] = lambda: _Principal()

    response = TestClient(app).get("/demo/datasets")

    assert response.status_code == 200
    names = [d["name"] for d in response.json()["datasets"]]
    assert names == ["silver$features", "gold$catalog"]  # bronze denied → dropped, order kept


@pytest.mark.asyncio
async def test_visible_batch_payload_is_deduped(spy: _Spy) -> None:
    """Duplicate names must collapse before the payload reaches OpenFGA."""

    app = FastAPI()
    app.state.fga = object()
    request = Request({"type": "http", "app": app, "headers": []})
    datasets = DatasetFilter(request, _settings(), _Principal())

    visible = await datasets.visible(["silver$a", "silver$a", "gold$b", "silver$a"])

    assert visible == {"silver$a", "gold$b"}
    assert len(spy.batches) == 1
    assert sorted(spy.batches[0]) == ["table:gold$b", "table:silver$a"], f"duplicate-laden batch payload: {spy.batches[0]}"
