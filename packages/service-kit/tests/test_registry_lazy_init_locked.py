"""SK-06 — the lazy DatasetRegistry build in ``dataset_handle`` is lock-guarded.

Sync route handlers run in the threadpool, so two concurrent FIRST requests both
observe ``state.registry is None`` and both construct a registry — the exact
hazard ``voice_encoder_lock`` already guards on the neighbouring slot. The loser's
registry (and its warm cache) is discarded; the guard makes first-build happen
once, like every other lazy slot on AppState.
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from service_kit.lancekit import registry as registry_mod
from service_kit.media import state as state_mod
from service_kit.media.config import Settings
from service_kit.media.state import AppState


def test_concurrent_first_requests_build_exactly_one_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[int] = []
    # Both threads must be INSIDE the constructor at once for the race to be real;
    # the barrier proves the overlap (unguarded) or times out broken (guarded —
    # the second thread never enters, which is the fix working).
    overlap = threading.Barrier(2, timeout=0.5)

    class RacedRegistry:
        def __init__(self, *args: object, **kwargs: object) -> None:
            built.append(threading.get_ident())
            with contextlib.suppress(threading.BrokenBarrierError):
                overlap.wait()

        def default(self) -> str:
            return "handle"

        def get(self, dataset_id: str) -> str:
            return "handle"

    # dataset_handle imports DatasetRegistry from the registry module per call,
    # so patching the module attribute intercepts the construction.
    monkeypatch.setattr(registry_mod, "DatasetRegistry", RacedRegistry)

    state = AppState(settings=Settings())
    results: list[object] = []

    def first_request() -> None:
        results.append(state_mod.dataset_handle(state))

    threads = [threading.Thread(target=first_request) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["handle", "handle"]
    assert len(built) == 1, f"unguarded lazy init let {len(built)} threads construct the registry"
