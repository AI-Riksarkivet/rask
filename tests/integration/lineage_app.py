"""Lineage over a live catalog: the pieces the provenance integration tests share (LH-468, ADR 0158).

Lineage keeps its feed and its edge list as tables of the catalog's provenance namespace, so every claim about
what it records is driven against the real catalog app on a real `dir` namespace, served by a live uvicorn
because lineage reaches it through the spec's REST client.

What stands in, and why each may: OpenFGA, as `openfga_grants.Grants` answering from a set of granted relations, and
the verified caller, as lineage's `authenticate` reading the principal key a test names in a header. Authorization is the
claim of none of the tests that use them; what the doors record is.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from collections.abc import AsyncGenerator, Generator, Mapping
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
import uvicorn
from fastapi import Body, FastAPI, Header, Response
from fastapi.testclient import TestClient
from openfga_sdk import OpenFgaClient

from service_kit.governed.decider import OpenFgaDecider
from service_kit.governed.fga import user_object
from service_kit.governed.machine_identity import ServicePrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.person_identity import PersonPrincipal
from service_kit.governed.principal import Principal
from service_kit.lakehouse.ns_errors import install_problem_handlers
from tests.integration.openfga_grants import Grants


if TYPE_CHECKING:
    from lineage.services.provenance import ProvenanceStore

#: The header a test names the caller's principal key in; lineage's `authenticate` is overridden to read it.
SUBJECT_HEADER = "x-test-subject"
#: The relay tick's binding name, which `governed_lineage` mounts the tick at, as the chart names it.
RELAY = "lineage-relay-cron"


@contextmanager
def live_catalog(root: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Generator[str]:
    """The catalog app under a live uvicorn on a real `dir` namespace at ``root``; yields its base URL."""
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(root),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "RASK_OIDC_ENABLED": "false",
        "RASK_FGA_ENABLED": "false",
        "LANCE_PROVENANCE_NAMESPACE": "provenance",
        **env,
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    try:
        with serve(app) as url:
            yield url
    finally:
        get_settings.cache_clear()


@contextmanager
def live_lineage(catalog_url: str, monkeypatch: pytest.MonkeyPatch, *, port: int = 0) -> Generator[str]:
    """Lineage's own app and lifespan under a live uvicorn, its store the catalog at ``catalog_url``; yields its base URL.

    ``port`` is for a catalog that must be told where lineage listens before lineage starts (`free_port`).
    """
    monkeypatch.setenv("LINEAGE_CATALOG_URL", catalog_url)
    from lineage.core.config import get_settings

    get_settings.cache_clear()
    from lineage.main import app

    try:
        with serve(app, port) as url:
            yield url
    finally:
        get_settings.cache_clear()


def free_port() -> int:
    """A port nothing listens on now, for a server another must be configured to reach before it starts."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def serve(app: FastAPI, port: int = 0) -> Generator[str]:
    """``app`` under uvicorn on a thread, its lifespan run; yields its base URL once it accepts connections."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 150
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)


class GraphBackend:
    """A graph backend's OpenLineage HTTP endpoint: it keeps what it takes, and answers 503 while it is down."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.down = False
        self.app = FastAPI()
        self.app.add_api_route("/api/v1/lineage", self._take, methods=["POST"])

    async def _take(self, event: dict[str, Any] = Body()) -> Response:  # noqa: B008 — FastAPI's own body declaration
        if self.down:
            return Response(status_code=503)
        self.received.append(event)
        return Response(status_code=201)


@contextmanager
def graph_backend() -> Generator[tuple[GraphBackend, str]]:
    """A :class:`GraphBackend` served on a socket; yields it and its base URL, what `LINEAGE_FORWARD_URL` names."""
    backend = GraphBackend()
    with serve(backend.app) as url:
        yield backend, url


def service(name: str) -> ServicePrincipal:
    """A pipeline's service principal, `etl` namespace."""
    return ServicePrincipal(service_account=f"system:serviceaccount:etl:{name}")


@contextmanager
def governed_lineage(catalog_url: str, monkeypatch: pytest.MonkeyPatch, granted: set[tuple[str, str, str]]) -> Generator[TestClient]:
    """Lineage's router behind its real authentication-then-FGA gate, recording into the catalog at ``catalog_url``.

    The caller is the principal a request names by key in :data:`SUBJECT_HEADER`: a `kubernetes~` key is a service
    principal, any other a person, as a verified token would prove either; OpenFGA answers from ``granted``, whose
    subjects are principal keys. The relay tick is mounted at :data:`RELAY` behind its sidecar-token guard,
    with the forward client lineage's lifespan builds when a graph backend is configured.
    """
    from lineage.api.relay_cron import build_relay_cron_router
    from lineage.api.security import authenticate
    from lineage.api.v1.router import api_router
    from lineage.core.config import get_settings
    from lineage.services.names import CatalogNames
    from lineage.services.provenance import ProvenanceStore

    monkeypatch.setenv("LINEAGE_CATALOG_URL", catalog_url)
    monkeypatch.setenv("RASK_FGA_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ISSUER", "https://idp.invalid/dex")
    monkeypatch.setenv("RASK_OIDC_AUDIENCE", "rask")
    monkeypatch.setenv("RASK_OIDC_CONNECTORS", '{"local": "dex"}')
    get_settings.cache_clear()
    store = ProvenanceStore.from_settings(get_settings())
    ensure_tables(store)

    def caller(subject: str = Header(alias=SUBJECT_HEADER)) -> PersonPrincipal | ServicePrincipal:
        principal = Principal.parse(subject)
        if principal.is_service:
            return ServicePrincipal(service_account=principal.subject)
        claims = IDToken(iss="https://idp.invalid/dex", sub=principal.subject, aud="rask", exp=4102444800, iat=0)
        return PersonPrincipal(principal=principal, claims=claims)

    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.forward_client = httpx.AsyncClient(timeout=settings.forward_timeout_seconds) if settings.forward_url else None
        try:
            yield
        finally:
            if app.state.forward_client is not None:
                await app.state.forward_client.aclose()

    app = FastAPI(lifespan=lifespan)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(api_router)
    app.include_router(build_relay_cron_router(RELAY))
    app.dependency_overrides[authenticate] = caller
    grants = Grants()
    grants.allowed = {(user_object(subject), relation, obj) for subject, relation, obj in granted}
    app.state.decider = OpenFgaDecider(cast(OpenFgaClient, grants))
    app.state.provenance = store
    app.state.names = CatalogNames.from_settings(settings)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()


def ensure_tables(store: ProvenanceStore, *, deadline_seconds: float = 30.0) -> None:
    """Create the store's tables once the catalog's boot task has created their namespace, as lineage's lifespan waits for it."""
    deadline = time.monotonic() + deadline_seconds
    while True:
        try:
            asyncio.run(store.ensure())
            return
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def run_event(run_id: str, state: str, at: str, *, outputs: list[str], inputs: tuple[str, ...] = (), facets: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """An OpenLineage RunEvent naming catalog tables by id, each under its top namespace."""
    return {
        "eventType": state,
        "eventTime": at,
        "producer": "https://example.invalid/etl",
        "run": {"runId": run_id, "facets": dict(facets or {})},
        "job": {"namespace": "etl", "name": "daily"},
        "inputs": [{"namespace": name.split("$")[0], "name": name} for name in inputs],
        "outputs": [{"namespace": name.split("$")[0], "name": name} for name in outputs],
    }
