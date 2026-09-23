"""A governed service that boots before the authorization store exists adopts it when it appears.

On a FRESH estate the catalog is the one service that provisions the OpenFGA store, and every other
governed service boots concurrently with it. Resolving once at boot made a lost race permanent:
`fga.resolve` returned None (or OpenFGA was not answering yet), `app.state.fga` stayed None for the
pod's lifetime, and every governed route answered 503 "enabled but unavailable" until someone restarted
it. Measured 2026-09-23 on a fresh install: fourteen Deployments latched that way — lineage,
maintenance, the medallion producer and stage runners, compute, notifications, the media trio and more.

So a failed resolution keeps resolving in the background, read-only, with a capped backoff, and assigns
the client the moment one exists. Consumers are unchanged: they still read `OpenFgaClient | None` — the
None just stops being permanent. A pinned store is used as-is, FGA-off builds nothing, and a pending
resolver is cancelled on dispose.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from service_kit.governed import auth_lifespan
from service_kit.governed import fga as fga_mod
from service_kit.governed import oidc as oidc_mod
from service_kit.governed.auth_lifespan import attach_auth, attach_fga


class _Settings(BaseModel):
    oidc_enabled: bool = False
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_discovery_url: str | None = None
    oidc_cache_ttl: int = 300
    oidc_leeway: int = 30
    oidc_allow_insecure: bool = False
    fga_enabled: bool = True
    fga_api_url: str = "http://fga.test:8080"
    fga_store_id: str | None = None
    fga_model_id: str | None = None
    fga_timeout_seconds: float = 5.0


class _Client:
    """The two methods the lifecycle touches."""

    def __init__(self, store_id: str) -> None:
        self.store_id = store_id
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Estate:
    """An OpenFGA whose store appears after `absent_for` lookups, recording every call."""

    def __init__(self, absent_for: int, *, raise_while_absent: bool = False) -> None:
        self.absent_for = absent_for
        self.raise_while_absent = raise_while_absent
        self.calls: list[str] = []

    async def resolve(self, api_url: str, **_: object) -> tuple[str, str] | None:
        self.calls.append("resolve")
        if self.calls.count("resolve") <= self.absent_for:
            if self.raise_while_absent:
                raise ConnectionError("fga not answering yet")
            return None
        return ("01STORE", "01MODEL")

    async def provision(self, api_url: str, **_: object) -> tuple[str, str]:
        self.calls.append("provision")
        return ("01PROVISIONED", "01MODEL")

    def make_client(self, api_url: str, store_id: str, model_id: str, **_: object) -> _Client:
        self.calls.append(f"make_client:{store_id}")
        return _Client(store_id)


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_lifespan, "FGA_RESOLVE_RETRY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(auth_lifespan, "FGA_RESOLVE_RETRY_MAX_SECONDS", 0.02)


def _install(monkeypatch: pytest.MonkeyPatch, estate: _Estate) -> None:
    monkeypatch.setattr(fga_mod, "resolve", estate.resolve)
    monkeypatch.setattr(fga_mod, "provision", estate.provision)
    monkeypatch.setattr(fga_mod, "make_client", estate.make_client)
    monkeypatch.setattr(oidc_mod, "OIDCVerifier", lambda *a, **k: None)


async def _eventually(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_while_absent", [False, True], ids=["store-absent", "fga-unreachable"])
async def test_a_store_that_appears_after_boot_is_adopted_without_a_restart(monkeypatch: pytest.MonkeyPatch, raise_while_absent: bool) -> None:
    estate = _Estate(absent_for=3, raise_while_absent=raise_while_absent)
    _install(monkeypatch, estate)
    app = FastAPI()

    await attach_auth(app, _Settings(), service="t")
    assert getattr(app.state, "fga", None) is None, "nothing to adopt yet — the gate must still answer 503 at boot"

    adopted = await _eventually(lambda: getattr(app.state, "fga", None) is not None)
    try:
        assert adopted, f"the store appeared (after {estate.absent_for} lookups) but the service never adopted it: {estate.calls}"
        assert app.state.fga.store_id == "01STORE"
        assert "provision" not in estate.calls, "the background resolver authored a store — a reader must never provision"
    finally:
        await fga_mod.dispose(app)


@pytest.mark.asyncio
async def test_a_client_that_built_at_boot_starts_no_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    estate = _Estate(absent_for=0)
    _install(monkeypatch, estate)
    app = FastAPI()

    await attach_auth(app, _Settings(), service="t")

    assert app.state.fga.store_id == "01STORE"
    assert getattr(app.state, "fga_resolver", None) is None
    await fga_mod.dispose(app)


@pytest.mark.asyncio
async def test_fga_OFF_starts_no_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    estate = _Estate(absent_for=99)
    _install(monkeypatch, estate)
    app = FastAPI()

    await attach_auth(app, _Settings(fga_enabled=False), service="t")
    await asyncio.sleep(0.05)

    assert estate.calls == [], f"FGA is off, yet the store was looked up: {estate.calls}"
    assert getattr(app.state, "fga_resolver", None) is None


@pytest.mark.asyncio
async def test_dispose_cancels_a_resolver_still_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pod leaving before the store appears must not leave a task retrying against a closed loop."""
    estate = _Estate(absent_for=10_000)
    _install(monkeypatch, estate)
    app = FastAPI()

    await attach_auth(app, _Settings(), service="t")
    resolver = app.state.fga_resolver
    await fga_mod.dispose(app)

    assert resolver.done(), "dispose left the background resolver running"


@pytest.mark.asyncio
async def test_the_client_can_live_on_another_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    """`maintenance` holds its client as `app.state.fga_client` (its sweep runs from a cron route)."""
    estate = _Estate(absent_for=2)
    _install(monkeypatch, estate)
    app = FastAPI()

    await attach_fga(app, _Settings(), service="maintenance", attr="fga_client")

    try:
        assert await _eventually(lambda: getattr(app.state, "fga_client", None) is not None)
        assert getattr(app.state, "fga", None) is None
    finally:
        await fga_mod.dispose(app)
