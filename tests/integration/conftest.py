"""Integration fixtures.

The backend namespace is a ``MagicMock(spec=LanceNamespace)`` injected via
dependency override, so these tests exercise *our* layer only — routing,
identifier parsing, request assembly, serialization, and error mapping — never
lance's actual operations.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import LanceNamespace


@pytest.fixture
def fake_ns() -> MagicMock:
    return MagicMock(spec=LanceNamespace)


@pytest.fixture
def real_ns_client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose namespace is a REAL pylance ``dir`` backend rooted at a tmp dir.

    Needed for the create path: every create now routes through the direct 2.2 + stable-row-ids write
    (``dataplane._create_table_direct`` -> ``declare`` + real ``lance.write_dataset``), so a MagicMock ns can
    no longer stand in — the write actually happens. This also makes the create tests STRONGER: "backend
    create fails" becomes a real create-then-recreate conflict, and "overwrite" a real overwrite, instead of
    a mocked return value. It is exactly the migration the 2.1->2.2 fix required — the mock-only tests could
    never have caught that plain tables were silently landing at format 2.1.
    """
    from lance_namespace import connect

    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")

    from catalog.core.config import get_settings

    get_settings.cache_clear()
    ns = connect("dir", {"root": str(tmp_path)})

    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.main import app

    app.dependency_overrides[get_namespace] = lambda: ns
    app.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client(fake_ns: MagicMock, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # A local root keeps the lifespan's build_namespace cheap; requests use the
    # injected fake regardless (get_namespace is overridden). monkeypatch.setenv
    # restores the environment on teardown so tests stay order-independent.
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", "/tmp/lance-test-root")
    # Object-store credentials are required by Settings; the local-dir backend
    # ignores them, but they must be set for Settings() to construct.
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")

    from catalog.core.config import get_settings

    get_settings.cache_clear()

    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.main import app

    app.dependency_overrides[get_namespace] = lambda: fake_ns
    app.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()
