"""A warehouse's trash lists each trashed table and namespace its caller holds a grant on, and nothing else ([[LH-556]]).

`GET /management/v1/warehouses/{warehouse_id}/trash` answers every recoverable drop under the warehouse, so a caller who
has lost a dropped object's name finds it again, with its kind, the name it held, its uuid (which an undrop names when
several held one name) and its deadline. It asks `can_get_metadata` on each trashed object's own uuid, as the undrop and
tasks doors ask on the one they are addressed by, and fills its page from what the caller may see; another warehouse's
trash is never in it. `?namespace=` lists what was dropped beneath that name.

Against the catalog as deployed with warehouses on, a loopback Dex signing callers in and a real OpenFGA deciding on a
store of the test's own ([[LH-235]]).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pytest

from catalog.core.config import get_settings
from catalog.services import warehouses
from service_kit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


PROJECT, HERE, ELSEWHERE = "acme", "wh-here", "wh-elsewhere"
#: acme's admin, who made everything; bob, granted one table.
ADMIN, READER = "alice", "bob"


def _ok(catalog: Any, method: str, path: str, **kwargs: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog and the request's keywords
    answer = catalog.client.request(method, path, headers={**catalog.bearer(ADMIN), **kwargs.pop("headers", {})}, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
    return answer


def _table(catalog: Any, name: str) -> str:  # noqa: ANN401 — the conftest's GovernedCatalog
    """``name`` created with one row; the uuid the catalog minted for it."""
    body = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))
    _ok(catalog, "POST", f"/v1/table/{name}/create", content=body, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    return str(_ok(catalog, "POST", f"/v1/table/{name}/describe", json={}).json()["properties"]["rask.uuid"])


@pytest.fixture
def estate(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> tuple[Any, dict[str, str]]:
    """acme's two warehouses, each written where its door keeps it (the door provisions a bucket), alice its admin.

    Under ``wh-here``: ``sales``, from which ``sales$mine`` and ``sales$shared`` (bob reads it) were dropped, and ``gone``, a
    namespace dropped with its table ``gone$t``. Under ``wh-elsewhere``: ``far``, from which ``far$t`` was dropped.
    Answers the catalog and each dropped object's uuid by name.
    """
    monkeypatch.setenv("LANCE_WAREHOUSES_ENABLED", "true")
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    catalog = request.getfixturevalue("governed_catalog")
    catalog.store.write((catalog.person(ADMIN), "owner", "estate:rask"))
    _ok(catalog, "POST", "/management/v1/projects", json={"id": PROJECT})
    settings = get_settings()
    for warehouse in (HERE, ELSEWHERE):
        record = {
            "id": warehouse,
            "bucket": warehouse,
            "root_uri": settings.root,
            "project": PROJECT,
            "status": "active",
            "created_at": datetime.now(UTC).isoformat(),
        }
        warehouses.create_warehouse_record(settings.registry_root, settings.storage_options(), record)
        catalog.store.write((f"project:{PROJECT}", "project", f"warehouse:{warehouse}"))
    for warehouse, namespace in ((HERE, "sales"), (HERE, "gone"), (ELSEWHERE, "far")):
        _ok(catalog, "POST", f"/management/v1/warehouses/{warehouse}/namespaces", json={"namespace": namespace})
    uuids = {name: _table(catalog, name) for name in ("sales$mine", "sales$shared", "gone$t", "far$t")}
    uuids["gone"] = str(_ok(catalog, "POST", "/v1/namespace/gone/describe", json={}).json()["properties"]["rask.uuid"])
    _ok(catalog, "POST", "/management/v1/table/sales$shared/access/grant", json={"user": catalog.person(READER), "relation": "reader"})
    for name in ("sales$mine", "sales$shared", "far$t"):
        _ok(catalog, "POST", f"/v1/table/{name}/drop", json={})
    _ok(catalog, "POST", "/v1/namespace/gone/drop", json={"behavior": "Cascade"})
    return catalog, uuids


def _listed(catalog: Any, caller: str, namespace: str | None) -> list[tuple[str, str, str]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """Every entry the trash answers ``caller``, walked a page of one at a time, as ``(kind, name, uuid)``.

    Bounded at one page per trash record the estate holds, plus the last: a cursor that does not advance fails the walk.
    """
    entries: list[tuple[str, str, str]] = []
    params: dict[str, str | int] = {"limit": 1} | ({"namespace": namespace} if namespace else {})
    for _ in range(6):
        page = catalog.client.get(f"/management/v1/warehouses/{HERE}/trash", params=params, headers=catalog.bearer(caller))
        assert page.status_code == 200, page.text
        body = page.json()
        assert all(entry["expires_at"] > entry["dropped_at"] and entry["expired"] is False for entry in body["objects"]), body
        entries += [(entry["kind"], entry["id"], entry["uuid"]) for entry in body["objects"]]
        if not body.get("page_token"):
            return entries
        params["page_token"] = body["page_token"]
    raise AssertionError(f"the listing did not end within six pages: {entries}")


#: Every drop under ``wh-here``, as ``(kind, name, the name its uuid is kept under)``.
EVERY_DROP_HERE = [
    ("namespace", "gone", "gone"),
    ("table", "sales$mine", "sales$mine"),
    ("table", "sales$shared", "sales$shared"),
    ("table", "gone$t", "gone$t"),
]


@pytest.mark.parametrize(
    ("caller", "namespace", "authorized", "expected"),
    [
        pytest.param(ADMIN, None, True, EVERY_DROP_HERE, id="its-admin-sees-every-drop-under-it"),
        pytest.param(ADMIN, "gone", True, [("table", "gone$t", "gone$t")], id="a-namespace-lists-what-was-dropped-beneath-it"),
        pytest.param(READER, None, True, [("table", "sales$shared", "sales$shared")], id="a-reader-sees-only-the-table-granted"),
        pytest.param(READER, None, False, EVERY_DROP_HERE, id="with-authorization-off-every-drop-under-it-is-listed"),
    ],
)
def test_the_trash_lists_exactly_the_drops_its_caller_holds_a_grant_on(
    estate: tuple[Any, dict[str, str]], caller: str, namespace: str | None, authorized: bool, expected: list[tuple[str, str, str]]
) -> None:
    catalog, uuids = estate
    if not authorized:
        ungoverned = get_settings().model_copy(update={"fga_enabled": False})
        catalog.client.app.dependency_overrides[get_settings] = lambda: ungoverned

    listed = _listed(catalog, caller, namespace)

    assert sorted(listed) == sorted((kind, name, uuids[key]) for kind, name, key in expected)
