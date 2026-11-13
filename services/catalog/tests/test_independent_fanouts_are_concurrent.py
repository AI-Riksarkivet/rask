"""catalog-api-11 — independent per-item I/O on the enumeration and guard paths runs CONCURRENTLY.

Three loops awaited a round trip per item with nothing between them that made the order matter:

* ``list_all_tables`` walks each warehouse's own directory catalog — an estate with N warehouses walked one at a
  time pays N sequential walks on the registry page every browser render;
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
from uuid import uuid4

import pytest
from lance_namespace import LanceNamespace

from catalog.api.fga_deps import GovernedObject
from catalog.api.pagination import MAX_LIST_LIMIT
from catalog.api.v1.endpoints import namespaces as n_ep
from catalog.api.v1.endpoints import tables as t_ep
from catalog.core.config import Settings
from catalog.services import table_claims
from lakehouse import location_claims
from lakehouse.control_events import ControlUuids
from lakehouse.storage_options import StorageOptions


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
    descendants = [GovernedObject(kind="table", segments=["bronze", f"t{i}"], uuid=str(uuid4())) for i in range(8)]

    asyncio.run(n_ep._require_descendants_unprotected(_settings(tmp_path), descendants, force=False))

    assert tracker.total == 8, f"only {tracker.total} protection records were read for 8 descendants"
    assert tracker.peak > 1, "the cascade's protection pre-flight reads one descendant at a time"


# ── the recoverable cascade's describe pass ───────────────────────────────────────────────────────


def test_the_trash_subtree_describes_every_table_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _Tracker()
    calls: list[str] = []
    settings = _settings(tmp_path)
    descendants = [GovernedObject(kind="table", segments=["bronze", f"t{i}"], uuid=str(uuid4())) for i in range(8)]
    claims = table_claims.claim_store(settings)
    for item in descendants:
        # Each table holds its location's claim under its uuid, as the door that made it left it: the cascade detaches
        # a table only under the claim its name resolves to.
        location_claims.take(
            claims, f"s3://bkt/{item.segments[-1]}.lance", "$".join(item.segments), item.segments, is_gone=lambda _claim: False, uuid=item.uuid
        )

    def native_call(_ns: Any, op: str, req: Any) -> Any:
        calls.append(op)
        if op == "describe_table":
            return _slow(tracker, SimpleNamespace(location=f"s3://bkt/{req.id[-1]}.lance"))()
        return SimpleNamespace(properties=None)

    monkeypatch.setattr(n_ep.native, "call", native_call)
    monkeypatch.setattr(n_ep.trash, "put", lambda *_a, **_kw: None)
    own = ControlUuids(object_uuid=str(uuid4()))

    asyncio.run(n_ep._trash_subtree(cast("LanceNamespace", object()), settings, None, ["bronze"], descendants, own=own, grace_days=7))

    assert tracker.total == 8, f"only {tracker.total} describes for 8 descendant tables"
    assert tracker.peak > 1, "the recoverable cascade describes its descendants one at a time"
    assert calls.count("deregister_table") == 8, "every descendant must still be detached"


# ── the estate table listing's per-warehouse walks ──────────────────────────────────────────────


def test_the_estate_listing_walks_every_warehouse_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _Tracker()
    monkeypatch.setattr(t_ep.warehouses, "served_ids", lambda *_a, **_kw: [f"wh{i}x" for i in range(8)])

    async def _connection(*_a: Any, **_kw: Any) -> object:
        return object()

    monkeypatch.setattr(t_ep, "namespace_for_warehouse", _connection)
    monkeypatch.setattr(t_ep, "_collect_tables", _slow(tracker, []))

    asyncio.run(
        t_ep.list_all_tables(
            request=cast("Any", SimpleNamespace()),
            settings=_settings(tmp_path),
            so=StorageOptions(),
            token=None,
            client=None,
            # With no caller and no OpenFGA the listing authorizes no candidate, so no name is resolved.
            resolver=cast("Any", None),
            limit=MAX_LIST_LIMIT,
        )
    )

    assert tracker.total == 8, f"only {tracker.total} walks for 8 warehouses"
    assert tracker.peak > 1, "the estate listing walks its warehouses one at a time"
