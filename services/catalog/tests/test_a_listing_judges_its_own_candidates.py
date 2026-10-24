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

from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))


def _ok(catalog: Any, path: str, caller: str = "alice", **kwargs: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog and the request's keywords
    method = catalog.client.get if path.endswith("/list") else catalog.client.post
    answer = method(path, headers={**catalog.bearer(caller), **kwargs.pop("headers", {})}, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
    return answer.json()


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's ``db`` holding ``a``, ``b``, ``c`` and ``hidden``; bob reads 1,500 tables elsewhere, then is granted
    ``reader`` on all of ``db``'s but ``hidden``.

    Written in that order so an estate-wide answer cut at OpenFGA's ceiling would hold the tables elsewhere first.
    """
    _ok(governed_catalog, "/v1/namespace/db/create", json={})
    for table in ("a", "b", "c", "hidden"):
        _ok(governed_catalog, f"/v1/table/db${table}/create", content=_ROWS, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    governed_catalog.store.write(*((governed_catalog.person("bob"), "reader", f"table:{uuid.uuid4()}") for _ in range(1500)))
    for table in ("a", "b", "c"):
        _ok(governed_catalog, f"/management/v1/table/db${table}/access/grant", json={"user": "dex~bob", "relation": "reader"})
    return governed_catalog


def test_a_caller_reaching_more_than_a_thousand_tables_lists_a_namespace_completely(estate: Any) -> None:  # noqa: ANN401
    listed = _ok(estate, "/v1/namespace/db/table/list", caller="bob")

    assert listed["tables"] == ["a", "b", "c"]


def test_a_page_counts_only_the_tables_the_caller_can_read(estate: Any) -> None:  # noqa: ANN401
    first = _ok(estate, "/v1/namespace/db/table/list", caller="bob", params={"limit": 2})
    rest = _ok(estate, "/v1/namespace/db/table/list", caller="bob", params={"limit": 2, "page_token": first.get("page_token") or ""})

    assert (first["tables"], first.get("page_token"), rest["tables"], rest.get("page_token")) == (["a", "b"], "b", ["c"], None)


def test_a_table_no_uuid_identifies_is_left_out_of_the_estate_listing_for_every_caller(estate: Any, tmp_path: Path) -> None:  # noqa: ANN401
    # A row at a location no claim names, written past the doors: the shape measured live on 2026-10-24, where it answered
    # this listing 500 to every caller.
    connect("dir", {"root": str(tmp_path)}).register_table(RegisterTableRequest(id=["db", "lost"], location="held"))

    answers = {caller: estate.client.get("/v1/table", headers=estate.bearer(caller)) for caller in ("alice", "bob")}

    assert {caller: (answer.status_code, answer.json().get("tables")) for caller, answer in answers.items()} == {
        "alice": (200, ["db$a", "db$b", "db$c", "db$hidden"]),
        "bob": (200, ["db$a", "db$b", "db$c"]),
    }


def test_a_grantee_of_one_namespace_does_not_see_its_sibling(estate: Any) -> None:  # noqa: ANN401
    for child in ("seen", "sibling"):
        _ok(estate, f"/v1/namespace/db${child}/create", json={})
    _ok(estate, "/management/v1/namespace/db$seen/access/grant", json={"user": "dex~carol", "relation": "reader"})

    listed = _ok(estate, "/v1/namespace/db/list", caller="carol")

    assert listed["namespaces"] == ["seen"]
