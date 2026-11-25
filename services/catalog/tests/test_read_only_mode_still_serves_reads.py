"""Read-only maintenance mode must refuse WRITES, not the entire read surface, and no name may read as a read.

The Lance Namespace route grammar is `POST /v1/<object>/{id}/<action>`: the spec puts the ACTION in the path, not in the
verb, so `describe`, `exists`, `count_rows`, `query`, `stats`, `version/list`, `tags/list`, `index/list`,
`policy/describe` and `access/my-permissions` are all POSTs. A window that refused every POST would make the catalog
unreadable, which is the outage the window exists to avoid; so a POST is classified by the action of the ROUTE it
matches, from a READ allowlist. An action nobody has classified, and a request matching no route, are writes.

DELETE, PUT and PATCH are writes whatever they name. A path's last segment can be a name a caller chose
(`DELETE /management/v1/projects/list`), so the path text never classifies a request: only the route it matches does.

The catalog's timers write too (an erasure release deletes tags and branches, the manifest reclaim deletes versions),
and they run inside the process, behind no mount. Through a window every timer is held: it claims no window and runs
nothing, so the first window after the window ends does the work.
"""

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from catalog.api.timers import TIMERS_STATE
from lakehouse.timers import Outcome, Timer
from tests.app_state import app_of


#: Reads served as POST, on both mounts: the spec puts the action in the path, and rask's management doors
#: follow the same grammar. A read-tier vend is a read (a direct reader's way in); a write-tier vend is not.
READS = [
    ("POST", "/v1/table/pages/describe"),
    ("POST", "/v1/table/pages/count_rows"),
    ("POST", "/management/v1/table/pages/changes"),
    ("POST", "/management/v1/table/pages/credentials?tier=read"),
    # A read whose path ends in a word the write doors also use: the tag's version is read, never set, here.
    ("POST", "/v1/table/pages/tags/version"),
    ("POST", "/management/v1/access/list-objects"),
    ("POST", "/management/v1/access/expand"),
    # What lineage asks before it records an outside producer's event: whether tuples govern a table, and which table
    # holds a location. Refused, every such event would be refused through the window.
    ("POST", "/management/v1/decisions/governed"),
    ("POST", "/management/v1/decisions/locate"),
    ("GET", "/management/v1/projects"),
]

#: Writes on both mounts, which must keep being refused: the spec's `/v1` and rask's `/management/v1`
#: (LH-446), where erasure, policy, grants and the registries live.
WRITES = [
    ("POST", "/v1/table/pages/insert"),
    # A name holding `?` ([[LH-526]]): the write is classified by the path routing matched, not one cut at the `?`.
    ("POST", "/v1/namespace/describe%3F/create"),
    ("POST", "/management/v1/table/pages/erasure"),
    ("POST", "/management/v1/warehouses"),
    ("POST", "/management/v1/table/pages/credentials?tier=write"),
    # A reclaim session deletes a trashed table's bytes: its door ends in the vend's word and names no tier.
    ("POST", "/management/v1/trash/table/5d0a6c0e-7c1b-4e0a-9f4e-3b1f6f0c2a55/credentials"),
    # Fail-closed: an action nobody has classified, and a path no route matches, are writes.
    ("POST", "/v1/table/pages/some_future_operation"),
    # A DELETE of an object named like a read action, and like the vend door at the read tier.
    ("DELETE", "/management/v1/projects/list"),
    ("DELETE", "/management/v1/projects/credentials?tier=read"),
]


@contextmanager
def _catalog_in_a_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Generator[TestClient]:
    """The real catalog app with `LANCE_MAINTENANCE_READ_ONLY=true`, its namespace a local `dir` root, and ``env`` besides."""
    for name, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_MAINTENANCE_READ_ONLY": "true",
        **env,
    }.items():
        monkeypatch.setenv(name, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        get_settings.cache_clear()


@pytest.fixture
def read_only_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    with _catalog_in_a_window(monkeypatch, tmp_path) as client:
        yield client


@pytest.mark.parametrize(("method", "path"), READS)
def test_a_request_that_only_READS_survives_a_maintenance_window(read_only_catalog: TestClient, method: str, path: str) -> None:
    response = read_only_catalog.request(method, path, json={} if method == "POST" else None)
    assert response.status_code != 503, f"{method} {path} is a READ — refusing it makes the catalog unreadable, not read-only: {response.text}"


@pytest.mark.parametrize(("method", "path"), WRITES)
def test_a_request_that_WRITES_is_refused_through_the_window(read_only_catalog: TestClient, method: str, path: str) -> None:
    response = read_only_catalog.request(method, path, json={})
    assert (response.status_code, response.json().get("code")) == (503, 17), f"{method} {path} writes and must not pass a read-only window: {response.text}"
    assert response.headers.get("Retry-After"), dict(response.headers)


#: Every catalog timer's period, each set: a timer runs only where git gives it a period.
TIMER_PERIODS = {
    f"LANCE_{timer}_PERIOD_SECONDS": "60" for timer in ("CONTROL_RELAY", "ERASURE_RELEASE", "COMMIT_RECONCILE", "MANIFEST_RECLAIM", "DECLARATION_CONVERGE")
}


def test_every_catalog_timer_is_held_through_the_window(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _catalog_in_a_window(monkeypatch, tmp_path, **TIMER_PERIODS) as client:
        timers: dict[str, Timer] = getattr(app_of(client).state, TIMERS_STATE)
        assert client.portal is not None
        turns = {name: client.portal.call(timer.turn).outcome for name, timer in timers.items()}

    assert turns == dict.fromkeys(turns, Outcome.HELD)
    assert len(turns) == len(TIMER_PERIODS)
