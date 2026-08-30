"""catalog-api-18 — reading the authorization-model DSL never blocks the event loop, and happens once.

``GET /v1/access/model`` is ``async def`` and called ``fga.load_model_dsl()`` directly. That resolves
to a blocking ``Path.read_text()``, uncached — so every request for the model text stalled the WHOLE
worker for a filesystem read, k8s probes included, on the estate's highest-privilege router. The file
is baked into the image and cannot change while the process runs, so the read is both avoidable and
repeatable-for-nothing.

Driven through the real handler with a recording loader: the assertion is about WHICH THREAD the
blocking read runs on and HOW MANY times it runs, not about the shape of the source.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from typing import Any, cast

import pytest
from openfga_sdk.client import OpenFgaClient

from catalog.api.v1.endpoints import access_admin as ep
from catalog.core.config import Settings


class _Client:
    def get_authorization_model_id(self) -> str:
        return "01MODEL"


def _settings() -> Settings:
    return cast("Settings", type("S", (), {"fga_model_id": None})())


@pytest.fixture(autouse=True)
def _isolate_the_cache() -> Iterator[None]:
    """The DSL is cached per process, so a faked read would otherwise outlive this module and hand
    ``test_access_admin``'s own model assertions the string ``"model dsl"``. Cleared on BOTH sides."""
    ep._model_dsl.cache_clear()
    yield
    ep._model_dsl.cache_clear()


def _drive(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[Any]]:
    """Call the real route twice; return (threads the read ran on, responses)."""
    threads: list[str] = []

    def recording_load() -> str:
        threads.append(threading.current_thread().name)
        return "model dsl"

    monkeypatch.setattr(ep.fga, "load_model_dsl", recording_load)

    async def _twice() -> list[Any]:
        client = cast("OpenFgaClient", _Client())
        return [await ep.get_access_model(client=client, settings=_settings()) for _ in range(2)]

    return threads, asyncio.run(_twice())


def test_the_drive_reaches_the_real_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the gate: a handler that never called the loader would satisfy both checks vacuously."""
    threads, responses = _drive(monkeypatch)
    assert threads, "the model loader was never called — the drive is not reaching the read"
    assert [r.dsl for r in responses] == ["model dsl", "model dsl"], [r.dsl for r in responses]


def test_the_blocking_read_does_not_run_on_the_event_loop_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    threads, _ = _drive(monkeypatch)
    main = threading.main_thread().name
    assert main not in threads, f"the model DSL is read on the event-loop thread ({main}) — hand it to a threadpool"


def test_the_model_dsl_is_read_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    threads, _ = _drive(monkeypatch)
    assert len(threads) == 1, f"the DSL was re-read {len(threads)} times for two requests — it is baked into the image, cache it"
