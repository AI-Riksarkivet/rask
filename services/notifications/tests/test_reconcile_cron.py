"""The reconciler's TICK — that the walk is actually REACHABLE, not merely written.

`test_reconciler.py` proves the walk is correct: it primes, it pages older, it holds the cursor back
on RETRY, it bounds itself. Every one of those tests constructs the client and the store itself, so
the whole suite stayed green while nothing in the running app ever called `reconcile()` — the feed
lane existed as library code and as tests, and as nothing else. That gap is what this file closes.

Four claims, and no lower layer can express any of them:

* the route is mounted **at the root**, at the binding name, on the app the image actually serves;
* `OPTIONS` acks Dapr's binding-discovery pre-flight (a 405 here means the component ticks and the
  app is never asked);
* the tick is **sidecar-only** — an unauthenticated trigger would drive unbounded catch-up walks
  against lineage using THIS service's governed identity;
* an unwired deployment **refuses** rather than walking without a cursor, because a walk that reads a
  missing store as "no cursor" primes the high-water mark to the newest row and silently drops every
  notification behind it.
"""

import importlib
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import metrics as notifications_metrics
from notifications.api import reconcile_cron
from notifications.api.ingest import DAPR_SUCCESS
from notifications.api.reconciler import (
    FeedPage,
    FeedRecord,
    LineageCursor,
    LineageCursorStore,
    LineageFeedBudgetExceeded,
    LineageFeedClient,
    ReconcileResult,
)
from notifications.api.settings import IngressSettings, get_ingress_settings
from notifications.config import get_notifications_settings


TOKEN = "cron-token"

#: The default in `IngressSettings.binding_name`, which the chart renders into both the Component name
#: and RASK_NOTIFICATIONS_BINDING_NAME. Spelled here rather than read from settings so a silent change
#: to that default fails this test instead of following it.
BINDING = "notifications-reconcile-cron"


def _count_ingest() -> Callable[..., Awaitable[dict[str, str]]]:
    """A no-op ingest returning the real DAPR_SUCCESS dict (reconcile compares against the constant)."""

    async def _ingest(*args: object, **kwargs: object) -> dict[str, str]:
        return DAPR_SUCCESS

    return _ingest


RUN_EVENT: dict[str, Any] = {
    "eventType": "COMPLETE",
    "eventTime": "2026-08-10T12:00:00+00:00",
    "run": {"runId": "run-1", "facets": {"author": {"name": "alice", "sub": "alice"}}},
    "outputs": [{"namespace": "bronze", "name": "bronze$pages"}],
}


class _FakeFeed:
    """Structural stand-in for `LineageFeedClient` — records what it was asked for."""

    def __init__(self, page: FeedPage) -> None:
        self._page = page
        self.calls: list[int | None] = []

    async def page(self, *, after: int | None) -> FeedPage:
        self.calls.append(after)
        return self._page


class _FakeCursor:
    """Structural stand-in for `LineageCursorStore`. `stored=None` is the first-ever-tick case."""

    def __init__(self, stored: LineageCursor | None = None) -> None:
        self.stored = stored
        self.writes: list[int] = []
        self.parked: list[tuple[int | None, int | None]] = []

    async def get(self) -> LineageCursor | None:
        return self.stored

    async def set(self, seq: int, *, resume_from: int | None = None, pending_high: int | None = None, floor: int | None = None, stalls: int = 0) -> None:
        # Mirrors the real signature so a fake cannot silently diverge from the store it stands in for.
        self.writes.append(seq)
        self.parked.append((resume_from, pending_high))


def _app(*, feed: _FakeFeed | None, cursor: _FakeCursor | None, actors: bool = True) -> FastAPI:
    """A bare app carrying only the cron router and the state the lifespan would have built.

    Deliberately not the module-level `app`: this exercises the ROUTE, and building it by hand is what
    lets a test express "the lifespan did not run" as a state that the route must refuse.
    """
    app = FastAPI()
    app.include_router(reconcile_cron.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.fga = None
    app.state.actors_registered = actors
    if feed is not None:
        app.state.lineage_feed = cast(LineageFeedClient, feed)
    if cursor is not None:
        app.state.lineage_cursor = cast(LineageCursorStore, cursor)
    return app


@pytest.fixture
def wired() -> tuple[TestClient, _FakeFeed, _FakeCursor]:
    feed = _FakeFeed(FeedPage(events=[FeedRecord(seq=42, event=RUN_EVENT)], next_cursor=None))
    cursor = _FakeCursor(stored=None)
    return TestClient(_app(feed=feed, cursor=cursor)), feed, cursor


@pytest.fixture
def token_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A deployment where daprd injected `APP_API_TOKEN` from `dapr.io/app-token-secret`."""
    monkeypatch.setenv("APP_API_TOKEN", TOKEN)
    yield


def test_the_cron_route_is_mounted_at_the_root_on_the_shipped_app() -> None:
    """The binding name IS the path, and it is at the ROOT — Dapr knows no api prefix.

    Asserted against the real module-level app rather than a hand-built one: a router that exists and
    is never included is exactly the shape this whole file exists to catch.
    """
    module = importlib.reload(importlib.import_module("notifications"))
    paths = module.app.openapi()["paths"]
    assert f"/{BINDING}" in paths
    assert "post" in paths[f"/{BINDING}"]
    # Not under the api prefix: a binding delivered to /api/... is a binding never delivered.
    assert not any(path.endswith(f"/{BINDING}") and path != f"/{BINDING}" for path in paths)


def test_options_acks_the_binding_discovery_preflight(wired: tuple[TestClient, _FakeFeed, _FakeCursor]) -> None:
    """Without this Dapr logs the app as not consuming the binding and delivers nothing, forever."""
    client, _, _ = wired
    assert client.options(f"/{BINDING}").status_code == 200


def test_a_first_tick_primes_the_cursor_and_notifies_nobody(wired: tuple[TestClient, _FakeFeed, _FakeCursor]) -> None:
    """The route reaches `reconcile()` with the lifespan's clients, and the walk runs."""
    client, feed, cursor = wired
    response = client.post(f"/{BINDING}")
    assert response.status_code == 200
    body = response.json()
    assert body["primed"] is True
    assert body["cursor"] == 42
    assert cursor.writes == [42]
    # Priming asks for the newest page, never for a range.
    assert feed.calls == [None]


@pytest.mark.usefixtures("token_env")
@pytest.mark.parametrize(
    "headers",
    [{}, {"dapr-api-token": "a-guess"}, {"dapr-api-token": ""}],
    ids=["no-token", "wrong-token", "empty-token"],
)
def test_the_tick_is_sidecar_only(wired: tuple[TestClient, _FakeFeed, _FakeCursor], headers: dict[str, str]) -> None:
    """An unauthenticated trigger would drive catch-up walks with this service's governed identity."""
    client, _, cursor = wired
    assert client.post(f"/{BINDING}", headers=headers).status_code == 403
    assert cursor.writes == []


@pytest.mark.usefixtures("token_env")
def test_the_matching_token_opens_the_door(wired: tuple[TestClient, _FakeFeed, _FakeCursor]) -> None:
    client, _, _ = wired
    assert client.post(f"/{BINDING}", headers={"dapr-api-token": TOKEN}).status_code == 200


@pytest.mark.usefixtures("token_env")
def test_a_public_front_door_may_not_trigger_a_tick(wired: tuple[TestClient, _FakeFeed, _FakeCursor]) -> None:
    """The measured gateway bypass: daprd stamps a valid token on requests it forwards for anyone."""
    client, _, _ = wired
    response = client.post(f"/{BINDING}", headers={"dapr-api-token": TOKEN, "dapr-caller-app-id": "gateway"})
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("feed", "cursor"),
    [(None, _FakeCursor()), (_FakeFeed(FeedPage()), None), (None, None)],
    ids=["no-feed-client", "no-cursor-store", "neither"],
)
def test_an_unwired_deployment_refuses_rather_than_walking_cursor_less(feed: _FakeFeed | None, cursor: _FakeCursor | None) -> None:
    """503 with a reason, never a 200 that primed the mark past everything it should have delivered."""
    client = TestClient(_app(feed=feed, cursor=cursor), raise_server_exceptions=False)
    assert client.post(f"/{BINDING}").status_code == 503


class TestIngressSettings:
    """The two fields the tick needed, and the one alias that admits this service to lineage."""

    def test_the_identity_is_read_from_the_var_the_chart_scans(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`services.yaml` builds LINEAGE_SERVICE_SUBJECTS from `env.RASK_LINEAGE_SERVICE_IDENTITY`.

        Reading a differently-named var here is how the door's allowlist and the caller's claim become
        two values a deployment can set to disagree.
        """
        monkeypatch.setenv("RASK_LINEAGE_SERVICE_IDENTITY", "notifications-prod")
        assert IngressSettings.model_validate({}).service_identity == "notifications-prod"

    def test_the_service_prefixed_name_still_overrides_this_service_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RASK_LINEAGE_SERVICE_IDENTITY", raising=False)
        monkeypatch.setenv("RASK_NOTIFICATIONS_SERVICE_IDENTITY", "just-me")
        assert IngressSettings.model_validate({}).service_identity == "just-me"

    def test_the_binding_name_defaults_to_the_component_the_chart_mints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RASK_NOTIFICATIONS_BINDING_NAME", raising=False)
        assert IngressSettings.model_validate({}).binding_name == BINDING

    def test_a_page_timeout_above_the_pass_budget_is_refused_at_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unsatisfiable by construction — and silent, since the timeout simply never fires."""
        monkeypatch.setenv("RASK_NOTIFICATIONS_FEED_TIMEOUT_SECONDS", "30")
        monkeypatch.setenv("RASK_NOTIFICATIONS_RECONCILE_BUDGET_SECONDS", "10")
        with pytest.raises(ValueError, match="may not outlast the whole pass"):
            IngressSettings.model_validate({})

    def test_the_shipped_defaults_satisfy_that_ordering(self) -> None:
        settings = IngressSettings.model_validate({})
        assert settings.feed_timeout_seconds <= settings.reconcile_budget_seconds


def test_the_lifespan_builds_the_clients_the_route_depends_on() -> None:
    """The other half of the wiring: `app.state` must actually carry what `get_feed_client` reads.

    Driven through the real module-level app's lifespan, so a lifespan that builds them under other
    names — or not at all — fails here rather than at the first tick in a cluster.
    """
    get_ingress_settings.cache_clear()
    module = importlib.reload(importlib.import_module("notifications"))
    with TestClient(module.app) as client:
        state = client.app.state  # type: ignore[attr-defined]
        assert isinstance(state.lineage_feed, LineageFeedClient)
        assert isinstance(state.lineage_cursor, LineageCursorStore)


class _NeverFreeLock:
    """A lock that is permanently held — stands in for a pass still in flight."""

    def locked(self) -> bool:
        return True


def test_an_overlapping_tick_is_skipped_rather_than_run_twice(wired: tuple[TestClient, _FakeFeed, _FakeCursor], monkeypatch: pytest.MonkeyPatch) -> None:
    """Dapr's cron has NO overlap protection: a pass that outruns its period gets a second delivery.

    Two concurrent passes read the same un-advanced cursor and re-walk the same rows, doubling FGA and
    actor load exactly when the slow thing is already slow. Both cron precedents carry this lock.
    """
    client, feed, cursor = wired
    monkeypatch.setattr(reconcile_cron, "_reconcile_lock", _NeverFreeLock())
    recorded = _CounterDouble()
    monkeypatch.setattr(notifications_metrics, "_feed_passes", recorded)

    response = client.post(f"/{BINDING}")

    assert response.status_code == 200
    assert response.json()["skipped"] is True
    assert feed.calls == [], "the skipped tick still walked the feed"
    assert cursor.writes == []
    assert recorded.by_outcome("skipped") == 1, (
        "the overlap branch is not counted, so a lane whose passes permanently outrun their 30s period is "
        "indistinguishable from a healthy one on EVERY PromQL surface — and PromQL is the only surface vmalert "
        f"can page from. The skip answers HTTP 200, so dapr_component_input_binding_count_total records "
        f'success="true" and the FastAPI span looks clean. Recorded: {recorded.calls}'
    )


class _CounterDouble:
    """Stands in for a module-level OTel counter.

    A counter-double rather than an in-memory MeterProvider on purpose: `MeterProvider` is process-global
    and set-once, and every metrics module here binds its instruments at import via a module-level
    `metrics.get_meter(...)`. Injecting a real provider would mean adding a rebinding hook to each module —
    more machinery than the counters under test.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        self.calls.append((amount, dict(attributes or {})))

    def by_outcome(self, outcome: str) -> int:
        return sum(amount for amount, attrs in self.calls if attrs.get("lance.notifications.outcome") == outcome)

    def outcomes(self) -> set[str]:
        return {attrs["lance.notifications.outcome"] for _, attrs in self.calls if "lance.notifications.outcome" in attrs}


def test_a_completed_tick_is_counted_and_BOTH_pass_outcomes_have_a_series(
    wired: tuple[TestClient, _FakeFeed, _FakeCursor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ratio alert needs a denominator that exists before the first skip.

    `sum(rate(...{outcome="skipped"})) / sum(rate(...))` reads "no data" — not zero — until BOTH series
    have been created, and a rule that cannot evaluate is indistinguishable from one that evaluates green.
    So a completed tick records `completed`=1 AND `skipped`=0, which is the convention lineage's own
    reconciler already states: adding 0 CREATES the series.
    """
    client, _feed, _cursor = wired
    recorded = _CounterDouble()
    monkeypatch.setattr(notifications_metrics, "_feed_passes", recorded)

    assert client.post(f"/{BINDING}").status_code == 200

    assert recorded.by_outcome("completed") == 1, f"a completed tick is not counted at all: {recorded.calls}"
    assert recorded.outcomes() == {"completed", "skipped"}, (
        "both pass outcomes must get a series from the first tick, or the skip-ratio alert reads no-data "
        f"until the first skip ever happens. Saw: {recorded.outcomes()}"
    )


def test_a_clean_pass_still_emits_the_gap_counter_so_the_SERIES_EXISTS(
    wired: tuple[TestClient, _FakeFeed, _FakeCursor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`notifications.feed.gaps` counts unread lineage rows destroyed by a prune — unrecoverable loss.

    It is declared, wired, and has NEVER created a table: it is only ever incremented on loss, so until the
    first gap the series does not exist and any alert over it reads "no data" forever. A silent-data-loss
    counter that is itself silent is worse than none — it reads as coverage.
    """
    client, _feed, _cursor = wired
    recorded = _CounterDouble()
    monkeypatch.setattr(notifications_metrics, "_feed_gaps", recorded)

    assert client.post(f"/{BINDING}").status_code == 200

    assert recorded.calls, (
        "a clean pass records nothing, so notifications_feed_gaps_total never comes into existence — verified "
        "absent from the live store's information_schema.tables"
    )
    assert sum(amount for amount, _ in recorded.calls) == 0, f"a clean pass must add 0, not a gap: {recorded.calls}"


def test_a_pass_that_outruns_its_budget_answers_503_not_500(wired: tuple[TestClient, _FakeFeed, _FakeCursor], monkeypatch: pytest.MonkeyPatch) -> None:
    """`asyncio.timeout` raises TimeoutError, which unhandled would leave the route an opaque 500.

    A tick that ran out of wall-clock is an availability fact about lineage, and has to read as one.
    """

    async def _timeout(**kwargs: object) -> ReconcileResult:
        raise LineageFeedBudgetExceeded("the lineage feed reconcile pass exceeded its 25.0s budget")

    monkeypatch.setattr(reconcile_cron, "reconcile", _timeout)
    client, _, _ = wired

    response = client.post(f"/{BINDING}")

    assert response.status_code == 503
    assert "budget" in response.text


def test_the_tick_hands_reconcile_its_configured_bounds(wired: tuple[TestClient, _FakeFeed, _FakeCursor], monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings-derived arguments are otherwise unobserved: every other route test primes, and
    `_prime()` returns before `max_pages` or `budget_seconds` is ever read."""
    seen: dict[str, object] = {}

    async def _spy(**kwargs: object) -> ReconcileResult:
        seen.update(kwargs)
        return ReconcileResult(scanned=3, cursor=99)

    monkeypatch.setattr(reconcile_cron, "reconcile", _spy)
    client, _, _ = wired
    settings = get_ingress_settings()

    assert client.post(f"/{BINDING}").json()["scanned"] == 3
    assert seen["max_pages"] == settings.feed_max_pages
    assert seen["budget_seconds"] == settings.reconcile_budget_seconds
    assert seen["open_inbox"] is not None


def test_a_catch_up_walk_runs_through_the_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not the priming branch: a stored cursor makes the route drive the real downward walk."""
    monkeypatch.setattr("notifications.api.reconciler.ingest_run_event", _count_ingest())
    feed = _FakeFeed(FeedPage(events=[FeedRecord(seq=9, event=RUN_EVENT), FeedRecord(seq=8, event=RUN_EVENT)], next_cursor=None))
    cursor = _FakeCursor(stored=LineageCursor(seq=7, updated_at=datetime.now(UTC)))
    client = TestClient(_app(feed=feed, cursor=cursor))

    body = client.post(f"/{BINDING}").json()

    assert body["primed"] is False
    assert body["scanned"] == 2, "the route did not drive a real catch-up walk"
    assert cursor.writes == [9]


def test_the_lifespan_wires_the_feed_client_from_settings_not_from_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """`isinstance` alone leaves base_url and identity unobserved — the two the chart must supply.

    Both have code defaults that are silently wrong in a cluster: `127.0.0.1:8000` is the pod itself,
    and an identity lineage's allowlist never learned 403s every tick.
    """
    monkeypatch.setenv("RASK_NOTIFICATIONS_LINEAGE_URL", "http://lineage.test:8000")
    monkeypatch.setenv("RASK_LINEAGE_SERVICE_IDENTITY", "notifications-under-test")
    get_ingress_settings.cache_clear()
    module = importlib.reload(importlib.import_module("notifications"))
    try:
        with TestClient(module.app) as client:
            feed = client.app.state.lineage_feed  # type: ignore[attr-defined]
            assert feed._base == "http://lineage.test:8000"
            assert feed._identity == "notifications-under-test"
    finally:
        get_ingress_settings.cache_clear()
        importlib.reload(importlib.import_module("notifications"))


def test_a_tick_without_an_actor_plane_refuses_instead_of_hanging() -> None:
    """MEASURED against a real uvicorn process, not reasoned about.

    The Dapr SDK builds its actor-proxy factory lazily, and that constructor calls a BLOCKING
    `DaprHealth.wait_for_sidecar()`. On a sidecar-less deployment — `make dev-micro` — the first
    delivery therefore pins the event loop for 60 s, during which the pass budget CANNOT fire because
    the loop it would fire on is the blocked one. The inbox routes already refuse without an actor
    plane; this route now does too, which is also just correct on its own terms: there is nowhere to
    deliver, so there is nothing worth walking the governed feed to produce.
    """
    feed = _FakeFeed(FeedPage(events=[FeedRecord(seq=42, event=RUN_EVENT)], next_cursor=None))
    cursor = _FakeCursor(stored=None)
    client = TestClient(_app(feed=feed, cursor=cursor, actors=False), raise_server_exceptions=False)

    response = client.post(f"/{BINDING}")

    assert response.status_code == 503
    assert feed.calls == [], "the feed was walked despite there being nowhere to deliver"
