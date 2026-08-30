"""ING-05 — `GET /ingests` authorizes the page in ONE round trip, not one per row.

The listing spans tenants, so each row is checked against its OWN project. It did that with a
sequential `authorize_ingest` per record — up to 200 OpenFGA `check`s AND, on the user path, up to
200 JWT verifications, for one request. `authz.md`'s own rule is "prefer `batch_check` over many
`check`s when filtering", and `services/viewer` already lists that way.

These pin the round-trip COUNT, not merely the filtered result: a per-row loop produces exactly the
same body, so a test that asserts only on visibility passes either way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError

from ingest import create_app
from ingest.runs import RunRecord


if TYPE_CHECKING:
    from collections.abc import Iterator


SERVICE_TOKEN = "s3cr3t-service-token"
_BEARER = {"authorization": "Bearer t"}


class _Verifier:
    """Counts verifications: the per-row loop re-verified the SAME bearer for every record."""

    def __init__(self, sub: str | None) -> None:
        self._sub = sub
        self.verifications = 0

    def verify(self, raw: str) -> Any:
        self.verifications += 1
        if self._sub is None:
            from lance_namespace import UnauthenticatedError

            raise UnauthenticatedError("invalid token")
        return type("Tok", (), {"sub": self._sub})()


@pytest.fixture
def _oidc_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Auth ON for this test only.

    `get_auth_settings` is `lru_cache`d, so the clear has to bracket the test on BOTH sides: without
    the teardown clear, an `IngestAuthSettings` built with `LANCE_OIDC_ENABLED=true` stays cached
    after monkeypatch has restored the environment, and every later test in the session runs against
    a door it never configured. Measured: sixteen unrelated failures across three modules.
    """
    monkeypatch.setenv("APP_API_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("LANCE_OIDC_ENABLED", "true")
    monkeypatch.setenv("LANCE_OIDC_ISSUER", "https://issuer.test")
    monkeypatch.setenv("LANCE_OIDC_AUDIENCE", "rask")
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    from ingest.auth import get_auth_settings

    get_auth_settings.cache_clear()
    yield
    get_auth_settings.cache_clear()


async def _seed(app: Any, projects: list[str]) -> None:
    for i, project in enumerate(projects):
        await app.state.run_store.put(RunRecord(run_id=f"r{i}", project=project, dataset="pages", kind="test-src", status="COMPLETE"))


class _Spy:
    """Records every authorization call the door makes, by kind."""

    def __init__(self, allowed: set[str], *, outage: bool = False) -> None:
        self.allowed = allowed
        self.outage = outage
        self.checks: list[str] = []
        self.batches: list[list[str]] = []

    async def check(self, _client: object, *, user: str, relation: str, obj: str) -> bool:
        self.checks.append(obj)
        if self.outage:
            raise ServiceUnavailableError("fga down")
        return obj in self.allowed

    async def batch_check(self, _client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        self.batches.append(list(objects))
        if self.outage:
            raise ServiceUnavailableError("fga down")
        return {o: o in self.allowed for o in objects}


def _client(monkeypatch: pytest.MonkeyPatch, spy: _Spy, verifier: _Verifier, projects: list[str]) -> TestClient:
    import asyncio

    monkeypatch.setattr("ingest.auth.fga.check", spy.check)
    monkeypatch.setattr("ingest.auth.fga.batch_check", spy.batch_check)
    app = create_app()
    app.state.oidc = verifier
    app.state.fga = object()
    asyncio.run(_seed(app, projects))
    return TestClient(app)


def test_the_listing_asks_openfga_once_for_the_whole_page(_oidc_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    projects = [f"tenant-{i}" for i in range(25)]
    spy = _Spy(allowed={"project:tenant-3", "project:tenant-7"})
    verifier = _Verifier("alice")

    res = _client(monkeypatch, spy, verifier, projects).get("/api/ingests?limit=200", headers=_BEARER)

    assert res.status_code == 200, res.text
    assert sorted(r["project"] for r in res.json()["runs"]) == ["tenant-3", "tenant-7"]
    assert len(spy.batches) == 1, f"expected ONE batch_check for the page, got batches={spy.batches} checks={spy.checks}"
    assert spy.checks == [], f"the page still authorizes row by row: {spy.checks}"
    assert verifier.verifications == 1, f"the caller's bearer was verified {verifier.verifications} times for one request"


def test_the_batch_carries_each_project_once(_oidc_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty runs in two projects is a two-object batch, not a twenty-object one."""
    spy = _Spy(allowed={"project:a"})
    res = _client(monkeypatch, spy, _Verifier("alice"), ["a", "b"] * 10).get("/api/ingests?limit=200", headers=_BEARER)

    assert res.status_code == 200, res.text
    assert len(spy.batches) == 1, f"expected ONE batch_check, got {spy.batches}"
    assert sorted(spy.batches[0]) == ["project:a", "project:b"], spy.batches
    assert len(res.json()["runs"]) == 10


def test_an_authz_outage_is_still_503_not_an_empty_page(_oidc_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing's own docstring: a fail-closed outage rendered as `{"runs": []}` looks like an answer."""
    spy = _Spy(allowed=set(), outage=True)
    res = _client(monkeypatch, spy, _Verifier("alice"), ["a", "b"]).get("/api/ingests", headers=_BEARER)

    assert res.status_code == 503, res.text


def test_an_invalid_bearer_is_still_401_not_an_empty_page(_oidc_on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy(allowed=set())
    res = _client(monkeypatch, spy, _Verifier(None), ["a", "b"]).get("/api/ingests", headers=_BEARER)

    assert res.status_code == 401, res.text
