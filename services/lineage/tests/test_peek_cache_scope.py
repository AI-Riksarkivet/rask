"""The demo peek cache is app-instance state, not module-global state (F-LIN-15).

``demo.py`` held ``_PAYLOADS`` / ``_VERSION_FIELDS`` as module-level mutable dicts, mutated from the
threadpool read, with a test-only ``_reset_peek_cache()`` seam to clear them — process-global state
that bleeds across app instances (and across tests that forget the seam). The cache lives on
``app.state`` now, one ``PeekCache`` per app, and the reset seam is gone in favour of per-instance
fixtures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi import FastAPI, Request
from lineage.api.v1.endpoints import demo


def test_demo_module_holds_no_module_level_mutable_cache() -> None:
    """The defect itself: no mutable dict may live at module scope in the demo endpoint."""
    module_dicts = [name for name, value in vars(demo).items() if isinstance(value, dict) and not name.startswith("__")]
    assert module_dicts == [], f"module-global mutable caches: {module_dicts}"


def _request_for(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


def test_peek_cache_is_per_app() -> None:
    """Two apps get two caches; the same app gets the same one back (no cross-app bleed)."""
    app_a, app_b = FastAPI(), FastAPI()
    cache_a = demo.get_peek_cache(_request_for(app_a))
    cache_b = demo.get_peek_cache(_request_for(app_b))
    assert cache_a is not cache_b
    assert demo.get_peek_cache(_request_for(app_a)) is cache_a


def test_a_fresh_cache_instance_shares_no_peek_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second cache instance pays its own cold read — nothing is remembered process-globally."""
    uri = str(tmp_path / "events.lance")
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), uri)

    opens = [0]
    real = lance.dataset

    def counted(*args: Any, **kwargs: Any) -> Any:
        opens[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(demo.lance, "dataset", counted)

    demo._read_dataset(demo.PeekCache(), "bronze$events", uri, {}, 5)
    cold = opens[0]
    assert cold > 1  # probe + per-version walk

    opens[0] = 0
    demo._read_dataset(demo.PeekCache(), "bronze$events", uri, {}, 5)
    assert opens[0] == cold, "a fresh cache instance was served another instance's peek state"
