"""Read-only maintenance mode must refuse WRITES, not the entire read surface.

THE DEFECT. `is_mutating` classifies by HTTP METHOD — "anything but GET/HEAD/OPTIONS". That is the
right rule for an ordinary REST API and the wrong one here, because the Lance Namespace route grammar
is `POST /v1/<object>/{id}/<action>` and the spec puts the ACTION in the path, not in the verb. So
`describe`, `exists`, `count_rows`, `query`, `stats`, `version/list`, `tags/list`, `index/list`,
`policy/describe` and `access/my-permissions` are all POSTs.

Flipping `LANCE_MAINTENANCE_READ_ONLY=true` therefore does not produce a read-only catalog. It
produces an UNREADABLE one: every consumer's describe and count 503s while the feature's whole purpose
is to keep serving reads during a maintenance window. A safety switch that causes the outage it exists
to avoid is worse than not having it.

THE FIX IS FAIL-CLOSED, and that direction matters. Actions are classified from a READ allowlist, so
an action nobody has classified is treated as a write and refused. A new spec operation added tomorrow
is blocked by default rather than silently let through a maintenance window.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catalog.api.maintenance_mode import is_mutating


#: Reads served as POST, on both mounts: the spec puts the action in the path, and rask's management doors
#: follow the same grammar. A read-tier vend is a read (a direct reader's way in); a write-tier vend is not.
READS = [
    "/v1/table/pages/describe",
    "/v1/table/pages/count_rows",
    "/management/v1/table/pages/changes",
    "/management/v1/table/pages/credentials?tier=read",
    "/management/v1/access/list-objects",
    "/management/v1/access/expand",
]

#: Writes on both mounts, which must keep being refused: the spec's `/v1` and rask's `/management/v1`
#: (LH-446), where erasure, policy, grants and the registries live.
WRITES = [
    "/v1/table/pages/insert",
    # A name holding `?` ([[LH-526]]): the write is classified by the path routing matched, not one cut at the `?`.
    "/v1/namespace/describe%3F/create",
    "/management/v1/table/pages/erasure",
    "/management/v1/warehouses",
    "/management/v1/table/pages/credentials?tier=write",
]


@pytest.fixture
def read_only_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """The real catalog app with `LANCE_MAINTENANCE_READ_ONLY=true`, its namespace a local `dir` root."""
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("LANCE_MAINTENANCE_READ_ONLY", "true")
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    get_settings.cache_clear()


@pytest.mark.parametrize("path", READS)
def test_a_POST_that_only_READS_survives_a_maintenance_window(read_only_catalog: TestClient, path: str) -> None:
    response = read_only_catalog.post(path, json={})
    assert response.status_code != 503, f"{path} is a READ served as POST — refusing it makes the catalog unreadable, not read-only: {response.text}"


@pytest.mark.parametrize("path", WRITES)
def test_a_POST_that_WRITES_is_refused_through_the_window(read_only_catalog: TestClient, path: str) -> None:
    response = read_only_catalog.post(path, json={})
    assert response.status_code == 503, f"{path} writes and must not pass a read-only window: {response.status_code} {response.text}"
    assert response.headers.get("Retry-After"), dict(response.headers)


def test_an_UNCLASSIFIED_action_is_treated_as_a_write() -> None:
    """Fail-closed: a spec operation nobody has classified must not slip through the window.

    The opposite default would make every future write op a silent hole, discovered only when it
    corrupts something during a maintenance window.
    """
    assert is_mutating("POST", "/v1/table/pages/some_future_operation")


def test_is_mutating_classifies_methods() -> None:
    """With no action in the path the METHOD decides: GET/HEAD/OPTIONS are served through the window
    (dropping them refuses every plain GET, an unreadable catalog), every other verb is a write."""
    assert not is_mutating("GET")
    assert not is_mutating("head")
    assert not is_mutating("OPTIONS")
    assert is_mutating("POST")
    assert is_mutating("delete")
    assert is_mutating("PUT")
    assert is_mutating("PATCH")
