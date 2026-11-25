"""Lineage over a live catalog: the pieces the provenance integration tests share (LH-468, ADR 0158).

Lineage keeps its feed and its edge list as tables of the catalog's provenance namespace, so every claim about
what it records is driven against the real catalog app on a real `dir` namespace, served by a live uvicorn
because lineage reaches it through the spec's REST client.

What stands in, and why each may: the verified caller, as lineage's `authenticate` reading the principal key a test names
in a header, and, for a test whose claim is what the doors record rather than who may record it, OpenFGA, as
`openfga_grants.Grants` answering from a set of granted relations. A test whose claim is who is shown what names a real
OpenFGA store instead.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest
from fastapi import Body, FastAPI, Header, Response
from fastapi.testclient import TestClient
from openfga_sdk import OpenFgaClient

from lakehouse.events.bus import BusUnavailableError
from lakehouse.events.bus_delivery import Handle, Lane, Park, Settlement
from lakehouse.governed import fga_provision
from lakehouse.governed.decider import OpenFgaDecider
from lakehouse.governed.fga_ids import user_object
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.machine_identity import ServicePrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from lakehouse.naming import UUID_PROPERTY
from lakehouse.problems.ns_errors import install_problem_handlers
from tests.bus_double import BusDouble, Sent, session_holding
from tests.integration.openfga_grants import Grants
from tests.live_server import loop_of, serve


if TYPE_CHECKING:
    from catalog.api.ticks import Tick
    from lineage.api.relay import RelayReport
    from lineage.services.provenance import ProvenanceStore

#: The header a test names the caller's principal key in; lineage's `authenticate` is overridden to read it.
SUBJECT_HEADER = "x-test-subject"


#: The topic the catalog announces on and lineage's consumer takes, each service's own default.
LINEAGE_TOPIC = "lineage.events.v1"


class JoinedBus(BusDouble):
    """The one bus a live catalog and a live lineage share: what the catalog publishes on lineage's topic is handed to
    lineage's own consumer, on lineage's loop, before the publish is acknowledged, and what the bus was told about each
    delivery is kept in ``settled``. While ``away`` every publish is refused, as by a bus that does not answer, and
    kept in ``refused``; :meth:`hand_late` hands lineage one of those. A publish made while lineage consumes nothing is
    taken and handed to nobody."""

    def __init__(self) -> None:
        super().__init__()
        self.away = False
        self.refused: list[Sent] = []
        self.settled: list[Settlement] = []
        self._consumed_on: asyncio.AbstractEventLoop | None = None
        self._consumer = ""

    async def send(self, topic: str, body: bytes, *, message_id: str, timeout: float) -> None:
        if self.away:
            self.refused.append(Sent(topic=topic, message_id=message_id, envelope=json.loads(body)))
            raise BusUnavailableError("this process is not connected to the bus")
        await super().send(topic, body, message_id=message_id, timeout=timeout)
        if self._consumed_on is None or topic != LINEAGE_TOPIC:
            return
        handing = asyncio.run_coroutine_threadsafe(self.deliver(self._consumer, self.sent[-1].envelope, subject=topic), self._consumed_on)
        self.settled.append(await asyncio.wrap_future(handing))

    def hand_late(self, refused: Sent) -> Settlement:
        """Hand lineage a message this bus refused, as the outbox relay's publish of the event it staged delivers it;
        answers what the bus is told about the delivery."""
        assert self._consumed_on is not None, "lineage consumes nothing yet"
        return asyncio.run_coroutine_threadsafe(self.deliver(self._consumer, refused.envelope, subject=refused.topic), self._consumed_on).result(timeout=60)

    async def consume(self, lane: Lane, handle: Handle, *, park: Park) -> None:
        self._consumed_on, self._consumer = asyncio.get_running_loop(), lane.consumer
        try:
            await super().consume(lane, handle, park=park)
        finally:
            self._consumed_on = None


def joined_bus(monkeypatch: pytest.MonkeyPatch) -> JoinedBus:
    """Name the catalog and lineage of this test one :class:`JoinedBus`, the catalog announcing every write on it: call
    before :func:`live_catalog` and :func:`live_lineage`, whose lifespans then hold it in place of a connection."""
    import catalog.main
    import lineage.main

    bus = JoinedBus()
    for name, value in {"LANCE_LINEAGE_EMIT_ENABLED": "true", "RASK_BUS_URL": "nats://bus.test:4222", "RASK_BUS_USER": "catalog"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(catalog.main, "bus_session", session_holding(bus))
    monkeypatch.setattr(lineage.main, "bus_session", session_holding(bus))
    return bus


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
        "LANCE_PROVENANCE_NAMESPACE": "lance_catalog$provenance",
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
def live_lineage(catalog_url: str, monkeypatch: pytest.MonkeyPatch) -> Generator[str]:
    """Lineage's own app and lifespan under a live uvicorn, its store the catalog at ``catalog_url``; yields its base URL."""
    monkeypatch.setenv("LINEAGE_CATALOG_URL", catalog_url)
    from lineage.core.config import get_settings

    get_settings.cache_clear()
    from lineage.main import app

    try:
        with serve(app) as url:
            yield url
    finally:
        get_settings.cache_clear()


def catalog_tick[R](catalog_url: str, tick: Callable[[Tick], Awaitable[R]], *, timeout: float = 120, clock: Callable[[], datetime] | None = None) -> R:
    """One ``tick`` of the catalog :func:`live_catalog` serves at ``catalog_url``, run on that server's own event loop with
    the catalog's settings, as the catalog's timer runs it; answers what the tick reports. ``clock`` is the wall clock
    the tick judges a deadline by, the real one unless given."""
    from catalog.api.ticks import Tick
    from catalog.core.config import get_settings
    from catalog.main import app

    async def run() -> R:
        return await tick(Tick(app, get_settings(), clock=clock) if clock is not None else Tick(app, get_settings()))

    return asyncio.run_coroutine_threadsafe(run(), loop_of(catalog_url)).result(timeout=timeout)


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
    return ServicePrincipal.for_account(f"system:serviceaccount:etl:{name}")


@contextmanager
def governed_lineage(
    catalog_url: str,
    monkeypatch: pytest.MonkeyPatch,
    granted: set[tuple[str, str, str]],
    fga_store: Any = None,  # noqa: ANN401 — the root conftest's OpenFgaStore
) -> Generator[TestClient]:
    """Lineage's router behind its real authentication-then-FGA gate, recording into the catalog at ``catalog_url``.

    The caller is the principal a request names by key in :data:`SUBJECT_HEADER`: a `kubernetes~` key is a service
    principal, any other a person, as a verified token would prove either; OpenFGA answers from ``granted`` (held as ``app.state.grants``, so a test can write a tuple mid-flow), whose
    subjects are principal keys. With ``fga_store``, a real OpenFGA decides instead: ``granted`` are the rungs written to it
    (`model.fga`'s relations, not its ``can_*`` actions), and the decider asks it through a client the app's lifespan
    opens. The app holds the forward client lineage's lifespan builds when a graph backend is configured, so
    :func:`relay_tick` runs the relay over it.
    """
    from lineage.api.security import authenticate
    from lineage.api.v1.router import api_router
    from lineage.core.config import get_settings
    from lineage.services.names import CatalogNames
    from lineage.services.provenance import ProvenanceStore
    from lineage.services.signature import store_published_keys

    monkeypatch.setenv("LINEAGE_CATALOG_URL", catalog_url)
    monkeypatch.setenv("RASK_FGA_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ISSUER", "https://idp.invalid/dex")
    monkeypatch.setenv("RASK_OIDC_AUDIENCE", "rask")
    monkeypatch.setenv("RASK_OIDC_IDP_ID", "dex")
    monkeypatch.setenv("RASK_OIDC_CONNECTORS", '{"local": "dex"}')
    get_settings.cache_clear()
    store = ProvenanceStore.from_settings(get_settings())
    ensure_tables(store)

    def caller(subject: str = Header(alias=SUBJECT_HEADER)) -> PersonPrincipal | ServicePrincipal:
        principal = Principal.parse(subject)
        if principal.is_machine:
            return ServicePrincipal(principal=principal)
        claims = IDToken(iss="https://idp.invalid/dex", sub=principal.subject, aud="rask", exp=4102444800, iat=0)
        return PersonPrincipal(principal=principal, claims=claims)

    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.forward_client = httpx.AsyncClient(timeout=settings.forward_timeout_seconds) if settings.forward_url else None
        try:
            if fga_store is None:
                yield
            else:
                async with fga_provision.make_client(fga_store.url, fga_store.store_id, fga_store.model_id, token_file=None) as fga:
                    app.state.decider = OpenFgaDecider(fga)
                    yield
        finally:
            if app.state.forward_client is not None:
                await app.state.forward_client.aclose()

    app = FastAPI(lifespan=lifespan)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(api_router)
    app.dependency_overrides[authenticate] = caller
    if fga_store is None:
        grants = Grants()
        grants.allowed = {(user_object(subject), relation, obj) for subject, relation, obj in granted}
        app.state.decider = OpenFgaDecider(cast(OpenFgaClient, grants))
        app.state.grants = grants
    elif granted:
        fga_store.write(*((user_object(subject), relation, obj) for subject, relation, obj in granted))
    app.state.provenance = store
    app.state.names = CatalogNames.from_settings(settings)
    # The off value lineage's lifespan sets for a process the chart names no bus.
    app.state.bus = None
    app.state.published_keys = store_published_keys()
    try:
        with TestClient(app) as client:
            yield client
    finally:
        get_settings.cache_clear()


def relay_tick(lineage: TestClient) -> RelayReport:
    """One relay tick over the app ``lineage`` serves, run on that app's own event loop, as its timer runs one."""
    from lineage.api import relay
    from lineage.core.config import get_settings

    assert lineage.portal is not None, "the relay ticks inside the app's lifespan"
    return lineage.portal.call(relay.tick, cast(FastAPI, lineage.app), get_settings())


def served_relay_tick(lineage_url: str, *, timeout: float = 120) -> RelayReport:
    """One relay tick of the lineage :func:`live_lineage` serves at ``lineage_url``, run on that server's own event loop
    with lineage's settings, as its timer runs one; answers what the tick reports, or raises what it raised."""
    from lineage.api import relay
    from lineage.core.config import get_settings
    from lineage.main import app

    return asyncio.run_coroutine_threadsafe(relay.tick(app, get_settings()), loop_of(lineage_url)).result(timeout=timeout)


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


def declared(catalog_url: str, *tables: str) -> dict[str, str]:
    """Declare each of ``tables``, and its namespace, in the catalog at ``catalog_url``; answers the uuid the catalog minted
    for each, the object its grants are held by.

    Lineage's HTTP door resolves each name an event names to that uuid as the caller, and checks the grants on it
    ([[LH-524]]), so a table must exist for any event naming it to be admitted.
    """
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:
        for namespace in dict.fromkeys(table.rsplit("$", 1)[0] for table in tables):
            assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        for table in tables:
            assert catalog.post(f"/v1/table/{table}/declare", json={}).status_code == 200
        return {table: str(catalog.post(f"/v1/table/{table}/describe", json={}).json()["properties"][UUID_PROPERTY]) for table in tables}


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
