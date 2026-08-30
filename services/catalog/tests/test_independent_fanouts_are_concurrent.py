"""catalog-api-11 — independent per-item I/O on the enumeration and guard paths runs CONCURRENTLY.

Three loops awaited a round trip per item with nothing between them that made the order matter:

* ``list_all_tables`` listed each BOUND warehouse's seed namespace one at a time — an estate with N
  bound tenants paid N sequential ``list_tables`` calls on the registry page every browser render;
* ``_trash_subtree`` described every descendant table one at a time before filing any trash record;
* ``_require_descendants_unprotected`` read one protection record per descendant, sequentially, as the
  PRE-FLIGHT of a cascade — so the guard's cost scaled with the subtree it is protecting.

None of the three is order-sensitive: they are reads whose results are collected before anything is
decided. The DESTRUCTIVE loops next to them (the deregister/trash pair per child, and
``_destroy_subtree``'s drops) are deliberately left sequential and are not asserted here — see their
docstrings for why partial, ordered progress is the property they trade concurrency for.

Measured as MAX CONCURRENT IN FLIGHT through the real coroutines: a sequential loop can never exceed
one, so this cannot pass for the wrong reason.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import LanceNamespace

from catalog.api.v1.endpoints import namespaces as n_ep
from catalog.api.v1.endpoints import tables as t_ep
from catalog.core.config import Settings


class _Tracker:
    """Counts overlap. ``peak`` is the most calls ever in flight at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.total = 0

    def enter(self) -> None:
        with self._lock:
            self.live += 1
            self.total += 1
            self.peak = max(self.peak, self.live)

    def leave(self) -> None:
        with self._lock:
            self.live -= 1


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "root": f"file://{tmp_path}",
            "registry_root": f"file://{tmp_path}",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )


def _slow(tracker: _Tracker, result: Any = None):
    """A blocking stand-in with a real dwell time, so overlap is observable."""

    def call(*_a: Any, **_kw: Any) -> Any:
        tracker.enter()
        try:
            import time

            time.sleep(0.05)
            return result
        finally:
            tracker.leave()

    return call


# ── the cascade's protection pre-flight ───────────────────────────────────────────────────────────


def test_the_protection_preflight_reads_every_descendant_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _Tracker()
    monkeypatch.setattr(n_ep.protection, "get_protection", _slow(tracker, None))
    descendants = [("table", ["bronze", f"t{i}"]) for i in range(8)]

    asyncio.run(n_ep._require_descendants_unprotected(_settings(tmp_path), descendants, force=False))

    assert tracker.total == 8, f"only {tracker.total} protection records were read for 8 descendants"
    assert tracker.peak > 1, "the cascade's protection pre-flight reads one descendant at a time"


# ── the recoverable cascade's describe pass ───────────────────────────────────────────────────────


def test_the_trash_subtree_describes_every_table_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _Tracker()
    described = SimpleNamespace(location="s3://bkt/t.lance")
    calls: list[str] = []

    def native_call(_ns: Any, op: str, _req: Any) -> Any:
        calls.append(op)
        if op == "describe_table":
            return _slow(tracker, described)()
        return SimpleNamespace()

    monkeypatch.setattr(n_ep.native, "call", native_call)
    monkeypatch.setattr(n_ep.trash, "put", lambda *_a, **_kw: None)
    descendants = [("table", ["bronze", f"t{i}"]) for i in range(8)]

    asyncio.run(n_ep._trash_subtree(cast("LanceNamespace", object()), _settings(tmp_path), None, ["bronze"], descendants))

    assert tracker.total == 8, f"only {tracker.total} describes for 8 descendant tables"
    assert tracker.peak > 1, "the recoverable cascade describes its descendants one at a time"
    assert calls.count("deregister_table") == 8, "every descendant must still be detached"


# ── the estate table listing's bound-warehouse seeds ──────────────────────────────────────────────


def test_the_estate_listing_lists_every_bound_seed_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _Tracker()
    bound = [{"top_ns": f"ns{i}", "warehouse_id": "wh", "root_uri": "s3://bkt"} for i in range(8)]
    monkeypatch.setattr(t_ep.warehouses, "list_bindings", lambda *_a, **_kw: bound)

    def native_call(_ns: Any, op: str, _req: Any) -> Any:
        if op == "list_tables":
            return _slow(tracker, SimpleNamespace(tables=["t"]))()
        return SimpleNamespace(tables=[], page_token=None, context=None)

    monkeypatch.setattr(t_ep.native, "call", native_call)
    monkeypatch.setattr(t_ep, "_collect_tables", lambda *_a, **_kw: [])
    request = cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(namespace=object()))))

    asyncio.run(
        t_ep.list_all_tables(
            request=request,
            ns=cast("LanceNamespace", object()),
            settings=_settings(tmp_path),
            token=None,
            client=None,
        )
    )

    assert tracker.total == 8, f"only {tracker.total} seed listings for 8 bound namespaces"
    assert tracker.peak > 1, "the estate listing walks its bound warehouses one at a time"
