"""End-to-end tests against a running catalog.

Set ``LANCE_REST_E2E_URL`` (e.g. http://localhost:2333) to run; skipped otherwise.
``scripts/e2e_live.sh`` derives it — and the bearer and the warehouse below — from the deployed release.
"""

from __future__ import annotations

import os
import uuid

import pyarrow as pa
import pytest
import requests
from topology import assert_parent_exists, create_top_level


URL = os.environ.get("LANCE_REST_E2E_URL", "")
#: The estate is AUTHENTICATED (owner ruling 2026-08-26). Every ``/v1/*`` route sits behind the v1
#: router's ``authorize`` dependency, so an anonymous request never reaches a handler: it is 401 at the
#: door, and with a valid bearer it is 403 on any object the caller holds no rung on. Empty = an
#: auth-off bring-up, and then the header is simply omitted.
TOKEN = os.environ.get("LANCE_E2E_TOKEN", "")
AUTH = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
ARROW = {**AUTH, "content-type": "application/vnd.apache.arrow.stream"}
DELIMITER = os.environ.get("LANCE_E2E_DELIM", "$")

#: A HYPHEN, not an underscore. With warehouses enabled the top-level namespace has to arrive through
#: ``POST /v1/warehouses/{id}/namespaces``, which validates the name as bucket-safe
#: (``^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z``); ``e2e_ns`` is refused 400 there.
NS = "e2e-ns"

pytestmark = pytest.mark.e2e


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _fresh(namespace: str, stem: str) -> str:
    """A table id no previous run can have claimed.

    A FIXED id cannot be reused against a long-lived estate. ``LANCE_TRASH_GRACE_DAYS`` is 7 on the
    deployed release, so a plain drop is RECOVERABLE — the id keeps a trash record and its grants stay
    live — and ``require_no_live_trash`` refuses the next create at that id with 409 "a dropped table
    of that name is still recoverable until <date>". ``purge=true`` frees a name the caller drops here,
    but not one an earlier run left trashed (that needs an undrop first), so the id is fresh instead.
    """
    return f"{namespace}{DELIMITER}{stem}{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def base() -> str:
    if not URL:
        pytest.skip("set LANCE_REST_E2E_URL to run e2e tests")
    try:
        requests.get(f"{URL}/livez", timeout=5).raise_for_status()
    except Exception:
        pytest.skip(f"server not reachable at {URL}")
    return URL.rstrip("/")


@pytest.fixture
def namespace(base: str) -> str:
    """The suite's top-level namespace, through whichever door THIS estate admits.

    Function-scoped and idempotent (``adopt_existing``): the lifecycle test drops it at the end, so
    each test re-establishes it rather than depending on the order the two ran in.
    """
    assert_parent_exists(create_top_level(base, NS, AUTH), NS)
    return NS


def test_full_lifecycle(base: str, namespace: str) -> None:
    table = _fresh(namespace, "t")

    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "name": ["a", "b", "c"]})
    assert requests.post(f"{base}/v1/table/{table}/create?mode=overwrite", data=_ipc(rows), headers=ARROW).status_code == 200

    assert (
        requests.post(
            f"{base}/v1/table/{table}/insert?mode=append",
            data=_ipc(pa.table({"id": pa.array([4], pa.int64()), "name": ["d"]})),
            headers=ARROW,
        ).status_code
        == 200
    )
    assert int(requests.post(f"{base}/v1/table/{table}/count_rows", json={}, headers=AUTH).text) == 4

    query = requests.post(f"{base}/v1/table/{table}/query", json={"k": 10, "filter": "id >= 2", "vector": {}}, headers=AUTH)
    assert query.headers["content-type"].startswith("application/vnd.apache.arrow.file")
    assert pa.ipc.open_file(pa.BufferReader(query.content)).read_all().num_rows == 3

    assert requests.post(f"{base}/v1/table/{table}/update", json={"predicate": "id = 1", "updates": [["name", "'X'"]]}, headers=AUTH).status_code == 200
    assert requests.post(f"{base}/v1/table/{table}/tags/create", json={"tag": "v1", "version": 1}, headers=AUTH).status_code == 200

    # Cleanup, and `purge=true` is what makes it cleanup: an unpurged drop leaves a live trash record
    # holding the id and its grants for the whole grace window.
    assert requests.post(f"{base}/v1/table/{table}/drop?purge=true", headers=AUTH).status_code == 200
    assert requests.post(f"{base}/v1/namespace/{namespace}/drop", headers=AUTH).status_code == 200
