"""Shared fixtures for the maintenance suite: the catalog's own app, and recorders for the refusal counters."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from lance_namespace import connect

from maintenance.core import metrics


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The catalog app over a real pylance `dir` namespace, with drops going to the trash.

    Rooted under `tmp_path / "catalog"`, so a test may keep datasets the catalog never registered beside it.
    """
    root = str(tmp_path / "catalog")
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", root)
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.core.config import get_settings
    from catalog.main import app

    get_settings.cache_clear()
    ns = connect("dir", {"root": root})
    app.dependency_overrides[get_namespace] = lambda: ns
    app.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def plan_refused_added(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every amount `compaction.plan.refused` is asked to add."""
    seen: list[int] = []
    monkeypatch.setattr(metrics._plan_refused, "add", lambda amount, attributes=None, **_: seen.append(amount))
    return seen


@pytest.fixture
def refused_added(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any] | None]]:
    """Every `(amount, attributes)` the datasets-refused counter is asked to add."""
    seen: list[tuple[int, dict[str, Any] | None]] = []
    monkeypatch.setattr(metrics._refused, "add", lambda amount, attributes=None, **_: seen.append((amount, attributes)))
    return seen


@pytest.fixture
def parked_added(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any] | None]]:
    """Every `(amount, attributes)` the per-table `compaction.tables.parked` counter is asked to add."""
    seen: list[tuple[int, dict[str, Any] | None]] = []
    monkeypatch.setattr(metrics._tables_parked, "add", lambda amount, attributes=None, **_: seen.append((amount, attributes)))
    return seen
