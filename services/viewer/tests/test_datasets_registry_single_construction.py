"""SK-06 (surviving half) — the viewer's ``_registry`` must not race first-build.

``service_kit.media.state.dataset_handle`` guards its lazy ``DatasetRegistry``
build with ``state.registry_lock``; the viewer's enumeration path carried a
VERBATIM UNLOCKED COPY of the same construction. Sync work runs in the
threadpool, so two concurrent first ``GET /datasets`` calls both observe
``state.registry is None`` and both construct — the loser's registry (with
whatever Lance/S3 handles it already opened) is silently discarded.

The fix is de-duplication, not a second lock: ONE guarded lazy-init in
``service_kit.media.state`` that both ``dataset_handle`` and the viewer's
enumeration path go through. This test drives two threads into the viewer's
entry point and asserts exactly one construction, whichever module the
construction is reached through.
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from service_kit.lancekit import registry as registry_mod
from service_kit.media.config import Settings
from service_kit.media.state import AppState
from viewer.api.v1.endpoints import datasets as datasets_mod


def test_concurrent_first_enumerations_build_exactly_one_registry(monkeypatch: pytest.MonkeyPatch) -> None:
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

    # Patch the name at BOTH plausible construction sites: the shared lazy-init
    # imports ``DatasetRegistry`` from the registry module per call, and the
    # viewer module holds its own top-level binding — so the interception works
    # regardless of which module the construction lives in.
    monkeypatch.setattr(registry_mod, "DatasetRegistry", RacedRegistry)
    monkeypatch.setattr(datasets_mod, "DatasetRegistry", RacedRegistry)

    state = AppState(settings=Settings())

    def first_enumeration() -> None:
        datasets_mod._registry(state)

    threads = [threading.Thread(target=first_enumeration) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(built) == 1, f"unlocked lazy init let {len(built)} threads construct the registry"
