"""Lineage's own app under its own lifespan, its ingest lane handed deliveries by the test: what lineage's lane tests share.

The lifespan starts the lane exactly as a pod does (`lineage.main._take_deliveries`), on a `tests.bus_double.BusDouble`
held in place of the bus session, so a delivery runs lineage's own handling and its own park, and is settled by the
seam's own rule. What stands in is the provenance store (:class:`Feed`): the feed and the refusals table are tables of
the catalog's provenance namespace, and a test whose claim is what the catalog holds drives a live one
(`tests/integration/test_a_repaired_park_is_replayed_into_the_feed.py`).
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Awaitable, Callable, Collection, Generator, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from typing import Any, cast

import pytest
from lance_namespace import ServiceUnavailableError
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint
from pydantic import BaseModel, ConfigDict
from starlette.testclient import TestClient

from lakehouse.events.bus_delivery import Lane, Settlement
from lakehouse.governed import fga_provision
from lakehouse.governed.decider import AccessDecider, OpenFgaDecider
from lakehouse.problems import domain_errors
from lineage import main
from lineage.api.ingest_lane import CONSUMER
from lineage.core import metrics
from lineage.core.config import get_settings
from lineage.models import DatasetEvent, RunEvent
from tests.bus_double import BusDouble, session_holding


class Kept(BaseModel):
    """One row the refusals table was handed."""

    model_config = ConfigDict(frozen=True)

    run_id: str | None
    author: str | None
    reason: str
    event_json: str


class Feed:
    """The provenance store in memory, as lineage's start, its ingest lane and the bus door's gate reach it.

    ``held`` is the feed's rows by row key (`provenance.feed_key`), which `recorded_event` and `events_recorded` answer
    from; ``unrecordable`` is a feed whose write the catalog does not take, ``unreadable`` one the catalog does not answer
    a read of, and ``refusals_refused`` a refusals table that does not take a row. Every event `record` took is in
    ``recorded``, and every row `record_refusal` took in ``kept``, by its key.
    """

    def __init__(
        self,
        *,
        held: Mapping[str, dict[str, Any]] | None = None,
        unrecordable: bool = False,
        unreadable: bool = False,
        refusals_refused: bool = False,
    ) -> None:
        self.held = dict(held or {})
        self.unrecordable = unrecordable
        self.unreadable = unreadable
        self.refusals_refused = refusals_refused
        self.recorded: list[RunEvent | DatasetEvent] = []
        self.kept: dict[str, Kept] = {}

    async def ensure(self) -> None:
        return None

    async def ready(self) -> None:
        return None

    async def record(self, event: RunEvent | DatasetEvent, *, aliased: Collection[str]) -> None:
        if self.unrecordable:
            # The class the feed's write raises for a catalog that does not take it (`translate_catalog_errors`).
            raise domain_errors.ServiceUnavailableError("the catalog does not take the feed's write")
        self.recorded.append(event)

    async def run_author(self, run_id: str) -> str | None:
        del run_id
        return None

    async def recorded_event(self, key: str) -> dict[str, Any] | None:
        return self.held.get(key)

    async def events_recorded(self, keys: Collection[str]) -> set[str]:
        if self.unreadable:
            raise ServiceUnavailableError("the catalog does not answer for the feed")
        return {key for key in keys if key in self.held}

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        if self.refusals_refused:
            raise ServiceUnavailableError("the catalog does not answer for the refusals table")
        # The table's writer encodes every string of the row, and refuses one with no UTF-8 form (measured on the locked
        # pyarrow, 2026-11-14: `pa.Table.from_pylist` raises `UnicodeEncodeError` for a lone surrogate).
        for text in (outbox_key, run_id, author, reason, event_json):
            if text is not None:
                text.encode()
        self.kept[outbox_key] = Kept(run_id=run_id, author=author, reason=reason, event_json=event_json)


class Lineage:
    """Lineage's app, started, with the bus its lane takes deliveries from and the feed it records into."""

    def __init__(self, client: TestClient, bus: BusDouble, feed: Feed) -> None:
        self.client = client
        self.bus = bus
        self.feed = feed

    def deliver(self, envelope: Mapping[str, Any] | bytes, *, attempt: int = 1) -> Settlement:
        """Hand ``envelope`` to lineage's ingest lane as its ``attempt``-th delivery; answers what the bus is told."""
        portal = self.client.portal
        if portal is None:
            raise RuntimeError("lineage's lifespan is not running, so its lane takes no delivery")
        return portal.call(partial(self.bus.deliver, CONSUMER, envelope, attempt=attempt))

    def lane(self) -> Lane:
        """The lane lineage pulls its consumer with, as its lifespan gave it to the bus."""
        portal = self.client.portal
        if portal is None:
            raise RuntimeError("lineage's lifespan is not running, so it pulls no consumer")
        return portal.call(self.bus.lane_of, CONSUMER)


def governed(monkeypatch: pytest.MonkeyPatch, store: Any = None) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """Turn authorization on for the app :func:`started` builds, and OIDC on, which OpenFGA on requires.

    With ``store``, the lifespan builds the deployed OpenFGA client against it and every check is the real model's on
    the tuples the test seeds there. Without one no OpenFGA runs: the store answers the lifespan's one question, which
    model this image's is, so it builds its client at once, and a test's ``decider`` answers every check.
    """
    if store is not None:
        monkeypatch.setenv("RASK_FGA_API_URL", store.url)
        monkeypatch.setenv("RASK_FGA_STORE_ID", store.store_id)
    else:

        async def _the_store_holds_this_images_model(*_args: object, **_kwargs: object) -> tuple[str, str]:
            return "01JA0000000000000000000000", "01JA0000000000000000000001"

        monkeypatch.setattr(fga_provision, "resolve", _the_store_holds_this_images_model)
    for key, value in {
        "RASK_FGA_ENABLED": "true",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid/dex",
        "RASK_OIDC_AUDIENCE": "rask",
        "RASK_OIDC_IDP_ID": "dex",
        "RASK_OIDC_CONNECTORS": '{"local": "dex"}',
    }.items():
        monkeypatch.setenv(key, value)


def decided[T](store: Any, call: Callable[[OpenFgaDecider], Awaitable[T]]) -> T:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """Run ``call`` with a decider asking the real OpenFGA ``store``, its client opened on the loop ``call`` runs on."""

    async def run() -> T:
        async with fga_provision.make_client(store.url, store.store_id, store.model_id, token_file=None) as client:
            return await call(OpenFgaDecider(client))

    return asyncio.run(run())


@contextmanager
def started(monkeypatch: pytest.MonkeyPatch, feed: Feed, *, decider: AccessDecider | None = None) -> Generator[Lineage]:
    """Lineage's app under its lifespan on the environment the test set, recording into ``feed``.

    ``decider`` answers the gate's authorization questions in place of the OpenFGA client the lifespan builds, for a test
    whose environment turns authorization on; the decider is read per delivery, so it is in place before the first.
    """
    bus = BusDouble()
    monkeypatch.setattr(main.ProvenanceStore, "from_settings", lambda _settings: feed)
    monkeypatch.setattr(main, "bus_session", session_holding(bus))
    get_settings.cache_clear()
    with TestClient(main.app) as client:
        if decider is not None:
            main.app.state.decider = decider
        yield Lineage(client, bus, feed)


def measured(monkeypatch: pytest.MonkeyPatch, *names: str) -> Callable[[str], dict[frozenset[tuple[str, str]], int]]:
    """Lineage's counters ``names`` (their module attributes, `_events_processed` say) read through a real in-memory reader.

    The counters are made when `lineage.core.metrics` is imported. Its code is run again into a module object of its own
    under a meter of this reader's provider, and only the named counters are swapped onto the imported module, so each
    keeps the module's own name and nothing else is replaced: a reload would replace the module's enums, which every
    importer holds as it imported them. Answers, for a counter's metric name, each attribute set's count.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    spec = importlib.util.find_spec(metrics.__name__)
    assert spec is not None and spec.loader is not None, "the metrics module has no loader to run it again with"
    measuring = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(otel_metrics, "get_meter", lambda name, *_args, **_kwargs: provider.get_meter(name))
        spec.loader.exec_module(measuring)
    for name in names:
        monkeypatch.setattr(metrics, name, getattr(measuring, name))

    def counts(metric_name: str) -> dict[frozenset[tuple[str, str]], int]:
        found: dict[frozenset[tuple[str, str]], int] = {}
        data = reader.get_metrics_data()
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != metric_name:
                        continue
                    # A counter only ever yields `NumberDataPoint`, the member of the reader's point union that has a value.
                    for point in cast(Sequence[NumberDataPoint], metric.data.data_points):
                        found[frozenset((key, str(value)) for key, value in (point.attributes or {}).items())] = int(point.value)
        return found

    return counts


def outcome(outcome: str, door: str) -> frozenset[tuple[str, str]]:
    """The attribute set `lineage.events.processed` counts ``outcome`` at ``door`` under."""
    return frozenset({("lance.lineage.outcome", outcome), ("lance.lineage.door", door)})
