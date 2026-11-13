"""A listing authorizes the items it would answer, on the objects their names resolve to, and fills its page from them.

A listing route opens on the container's ``can_get_metadata``, which a grant on any one child below it lights (C1's
upward visibility), so the route opening says nothing about its contents: each item is judged on itself, a table on
``can_read_data`` and a namespace on ``can_get_metadata`` ([[LH-223]], [[LH-524]]). It never asks OpenFGA which objects
the caller reaches across the estate: that answer stops at the server's ceiling of 1,000 objects, and a caller holding
more than that elsewhere saw a small namespace listed short. Pagination counts only what the caller can see, so a page
is not cut before the items are judged.

An item whose uuid cannot be read, a table whose location no claim names among them, is left out of the page and logged,
so it never takes the listing down for the callers it was never answered to ([[LH-534]]).

Every decision is the real model's, on a store of this test's own ([[LH-235]]).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from lance_namespace import RegisterTableRequest, connect

from catalog.core.config import get_settings
from catalog.services import warehouses
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))


def _ok(catalog: Any, path: str, caller: str = "alice", **kwargs: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog and the request's keywords
    method = catalog.client.get if path.endswith("/list") else catalog.client.post
    answer = method(path, headers={**catalog.bearer(caller), **kwargs.pop("headers", {})}, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
    return answer.json()


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's ``lance_catalog$db`` holding ``a``, ``b``, ``c`` and ``hidden``; bob reads 1,500 tables elsewhere, then is granted
    ``reader`` on all of ``lance_catalog$db``'s but ``hidden``.

    Written in that order so an estate-wide answer cut at OpenFGA's ceiling would hold the tables elsewhere first.
    """
    assert governed_catalog.create_in_the_estate("alice", "db").status_code == 200
    for table in ("a", "b", "c", "hidden"):
        _ok(governed_catalog, f"/v1/table/lance_catalog$db${table}/create", content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    governed_catalog.store.write(*((governed_catalog.person("bob"), "reader", f"table:{uuid.uuid4()}") for _ in range(1500)))
    for table in ("a", "b", "c"):
        _ok(governed_catalog, f"/management/v1/table/lance_catalog$db${table}/access/grant", json={"user": "dex~bob", "relation": "reader"})
    return governed_catalog


def test_a_page_counts_only_the_tables_the_caller_can_read(estate: Any) -> None:  # noqa: ANN401
    # bob reaches 1,500 tables elsewhere, so the pages hold all of a, b and c only if the listing judged its own candidates.
    first = _ok(estate, "/v1/namespace/lance_catalog$db/table/list", caller="bob", params={"limit": 2})
    rest = _ok(estate, "/v1/namespace/lance_catalog$db/table/list", caller="bob", params={"limit": 2, "page_token": first.get("page_token") or ""})

    assert (first["tables"], first.get("page_token"), rest["tables"], rest.get("page_token")) == (["a", "b"], "b", ["c"], None)


def test_a_table_no_uuid_identifies_is_left_out_of_the_estate_listing_for_every_caller(estate: Any, tmp_path: Path) -> None:  # noqa: ANN401
    # A row at a location no claim names, written past the doors: the shape measured live on 2026-10-24, where it answered
    # this listing 500 to every caller.
    connect("dir", {"root": str(tmp_path)}).register_table(RegisterTableRequest(id=["lance_catalog", "db", "lost"], location="held"))

    answers = {caller: estate.client.get("/v1/table", headers=estate.bearer(caller)) for caller in ("alice", "bob")}

    assert {caller: (answer.status_code, answer.json().get("tables")) for caller, answer in answers.items()} == {
        "alice": (200, ["lance_catalog$db$a", "lance_catalog$db$b", "lance_catalog$db$c", "lance_catalog$db$hidden"]),
        "bob": (200, ["lance_catalog$db$a", "lance_catalog$db$b", "lance_catalog$db$c"]),
    }


def test_a_grantee_of_one_namespace_does_not_see_its_sibling(estate: Any) -> None:  # noqa: ANN401
    for child in ("seen", "sibling"):
        _ok(estate, f"/v1/namespace/lance_catalog$db${child}/create", json={})
    _ok(estate, "/management/v1/namespace/lance_catalog$db$seen/access/grant", json={"user": "dex~carol", "relation": "reader"})

    listed = _ok(estate, "/v1/namespace/lance_catalog$db/list", caller="carol")

    assert listed["namespaces"] == ["seen"]


@pytest.mark.parametrize(
    ("caller", "shown"),
    [
        pytest.param("bob", ["lance_catalog"], id="a-reader-of-tables-in-the-estates-warehouse-sees-it-alone"),
        pytest.param("dave", ["acme-wh"], id="a-reader-of-a-registered-warehouse-sees-it-alone"),
        pytest.param("erin", [], id="a-caller-holding-nothing-sees-no-warehouse"),
    ],
)
def test_the_root_lists_exactly_the_warehouses_its_caller_may_see(estate: Any, caller: str, shown: list[str]) -> None:  # noqa: ANN401
    """The root's children are the warehouses ([[LH-593]]): the estate's own, and each registered one, here ``acme-wh``
    and ``beta-wh``, of which dave reads the first."""
    settings = get_settings()
    for warehouse in ("acme-wh", "beta-wh"):
        record = {"id": warehouse, "bucket": warehouse, "project": "acme", "root_uri": f"{settings.root}/{warehouse}"}
        warehouses.put_warehouse(settings.registry_root, settings.storage_options(), record)
    estate.store.write((estate.person("dave"), "reader", "warehouse:acme-wh"))

    listed = _ok(estate, "/v1/namespace/$/list", caller=caller)

    assert listed["namespaces"] == shown
