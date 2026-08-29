"""The KG engine cache must be per-app and per-key, not one module global behind one lock (VS-08).

open_python-audit VS-08, two defects in one cache:

1. **Module-global mutable state.** `create_viewer_app` can build several apps over different
   `AppState`s (the test/composition seam this codebase deliberately has), and they all shared one
   dict and one `_CACHE_MAX = 2` budget. Nothing released the ~370 MB engines when an app was torn
   down, so a process that built two apps held both apps' engines for its whole life.

2. **One lock around the BUILD.** The lock was module-global and wrapped `_build_resources`, a
   ~20 s job. Every `/api/graph/*` request for ANY dataset — including the cheap `GET /status` —
   blocked in the threadpool for that whole window, and with 40 threadpool workers a handful of
   cold graph requests starve every other sync route in the process.

Driven through `get_status`, the cheapest route on the cache, because that is exactly the request
the finding says a cold build for an unrelated dataset must not hold up.
"""

from __future__ import annotations

import gc
import threading
import weakref
from typing import Any

import pytest
from viewer.api.v1.endpoints import graph as graph_ep

from service_kit.media.state import AppState


#: A cold build takes ~20 s in production; a test needs only "long enough that a serialized second
#: request cannot possibly finish first".
_BUILD_HOLD_S = 5.0
_OTHER_DATASET_BUDGET_S = 2.0


class _Handle:
    """The parts of a `DatasetHandle` the graph cache path touches."""

    def __init__(self, dataset_id: str) -> None:
        self.uri = f"memory://{dataset_id}"
        self.id = dataset_id
        self.storage_options: dict[str, str] = {}
        self.descriptor = _Descriptor()

    def table_uri(self, name: str) -> str:
        return f"{self.uri}/{name}"


class _Descriptor:
    declared: Any = None


class _Version:
    version = 7


class _Lance:
    """Stands in for the `lance` module: only `dataset(...).version` is read here."""

    @staticmethod
    def dataset(*_a: object, **_k: object) -> _Version:
        return _Version()


def _resources(clip_title_column: str | None = None) -> graph_ep._GraphResources:
    return graph_ep._GraphResources(
        engine=object(),
        ent_by_id={},
        ent_videos={},
        rels=[],
        n_mentions=0,
        n_videos=0,
        clip_title_column=clip_title_column,
    )


@pytest.fixture(autouse=True)
def _kg_tables_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every table the graph capability names is present, and its version is stable."""
    monkeypatch.setattr(
        graph_ep,
        "_kg_tables",
        lambda _declared: graph_ep._KgTables(entities="kg_entities", chunks="kg_chunks", mentions="kg_mentions", relationships="kg_relationships"),
    )
    monkeypatch.setattr(graph_ep.store, "exists", lambda *_a, **_k: True)
    monkeypatch.setattr(graph_ep, "lance", _Lance)
    monkeypatch.setattr(graph_ep, "dataset_handle", lambda _state, dataset=None: _Handle(dataset or "default"))


def _count_builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    built: list[str] = []

    def _build(handle: _Handle, _names: graph_ep._KgTables) -> graph_ep._GraphResources:
        built.append(handle.uri)
        return _resources()

    monkeypatch.setattr(graph_ep, "_build_resources", _build)
    return built


def test_two_apps_do_not_share_one_engine_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second app must build its OWN engine — it has its own registry, settings and lifetime."""
    built = _count_builds(monkeypatch)

    graph_ep.get_status(AppState(), dataset="isolation-corpus")
    graph_ep.get_status(AppState(), dataset="isolation-corpus")

    assert len(built) == 2, (
        f"the second app was served the first app's cached engine (builds: {built}) — one module-global dict is shared by every app in the process"
    )


def test_tearing_an_app_down_releases_its_engines(monkeypatch: pytest.MonkeyPatch) -> None:
    """~370 MB per engine: an app that is gone must not keep holding them."""
    # Held in a list so the only strong references are ones this test can drop: a plain local that
    # the patched builder closes over would keep the engine alive from the closure, not the cache.
    holder = [_resources()]
    monkeypatch.setattr(graph_ep, "_build_resources", lambda *_a, **_k: holder[0])
    alive = weakref.ref(holder[0])

    state = AppState()
    graph_ep.get_status(state, dataset="teardown-corpus")

    holder.clear()
    del state
    gc.collect()
    assert alive() is None, "the built engine outlived the app that built it — nothing releases the cache when an app is torn down"


def test_a_cold_build_for_one_dataset_does_not_block_another(monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding's second half: the lock was global, so B's cheap /status waited on A's ~20 s build."""
    started = threading.Event()
    release = threading.Event()

    def _build(handle: _Handle, _names: graph_ep._KgTables) -> graph_ep._GraphResources:
        if handle.id == "slow":
            started.set()
            release.wait(_BUILD_HOLD_S)
        return _resources()

    monkeypatch.setattr(graph_ep, "_build_resources", _build)
    state = AppState()

    slow = threading.Thread(target=lambda: graph_ep.get_status(state, dataset="slow"), daemon=True)
    slow.start()
    assert started.wait(_BUILD_HOLD_S), "the slow build never started"

    done = threading.Event()

    def _other() -> None:
        graph_ep.get_status(state, dataset="other")
        done.set()

    threading.Thread(target=_other, daemon=True).start()
    served = done.wait(_OTHER_DATASET_BUDGET_S)
    release.set()
    slow.join(timeout=_BUILD_HOLD_S)

    assert served, (
        f"a status request for an unrelated dataset was still blocked {_OTHER_DATASET_BUDGET_S}s into another dataset's cold build — the cache lock wraps the build, so one cold KG stalls every graph request in the process"
    )


def test_the_same_app_still_reuses_its_built_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-flight is the point of the cache: the same key must never build twice."""
    built = _count_builds(monkeypatch)
    state = AppState()

    graph_ep.get_status(state, dataset="reuse-corpus")
    graph_ep.get_status(state, dataset="reuse-corpus")

    assert len(built) == 1, f"the cache stopped memoizing — a ~20 s, ~370 MB build ran twice for one dataset version (builds: {built})"


def test_concurrent_first_requests_build_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The thundering-herd guarantee the global lock did provide must survive the per-key split."""
    built: list[str] = []
    gate = threading.Barrier(2, timeout=_BUILD_HOLD_S)

    def _build(handle: _Handle, _names: graph_ep._KgTables) -> graph_ep._GraphResources:
        built.append(handle.uri)
        return _resources()

    monkeypatch.setattr(graph_ep, "_build_resources", _build)
    state = AppState()

    def _request() -> None:
        gate.wait()
        graph_ep.get_status(state, dataset="herd-corpus")

    threads: list[threading.Thread] = [threading.Thread(target=_request) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=_BUILD_HOLD_S)

    assert len(built) == 1, f"two concurrent first requests both built the engine (builds: {built}) — single-flight is gone"
