"""An undrop restores exactly the trashed table its caller names, by uuid when several held the name.

A recoverable drop files its trash record under the table's uuid (ADR 0173 §8, §9, [[LH-525]]), so the name is free
again at once: a table created there is another uuid, holding none of the dropped one's grants ([[LH-524]]). The undrop
door stays addressed by name. With one trashed table of the name it restores that one; with several it answers 409
listing their uuids and deadlines, and restores the one ``?uuid=`` names with its data and grants; while a live table
holds the name it answers 409, since two tables cannot share it, and so it does while another namespace holds the name
of the one the table was dropped from, which its caller may hold no grant on.

Every decision is the real model's, on a store of this test's own ([[LH-235]]).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


def _ok(catalog: Any, path: str, caller: str = "alice", **kwargs: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog and the request's keywords
    answer = catalog.client.post(path, headers={**catalog.bearer(caller), **kwargs.pop("headers", {})}, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
    return answer


def _create(catalog: Any, rows: int) -> str:  # noqa: ANN401 — the conftest's GovernedCatalog
    """``db$t`` created with ``rows`` rows; the uuid the catalog minted for it."""
    body = encode_arrow_stream(pa.table({"id": pa.array(range(rows), pa.int64())}))
    _ok(catalog, "/v1/table/db$t/create", content=body, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    return str(_ok(catalog, "/v1/table/db$t/describe", json={}).json()["properties"]["rask.uuid"])


@pytest.fixture
def recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A grace period, so a drop files a trash record rather than destroying the table."""
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")


@pytest.fixture
def estate(recoverable: None, governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's namespace ``db``."""
    _ok(governed_catalog, "/v1/namespace/db/create", json={})
    return governed_catalog


def test_an_undrop_of_a_name_two_dropped_tables_held_asks_which_and_restores_the_one_named(estate: Any) -> None:  # noqa: ANN401
    first = _create(estate, rows=2)
    _ok(estate, "/management/v1/table/db$t/access/grant", json={"user": "dex~bob", "relation": "reader"})
    _ok(estate, "/v1/table/db$t/drop", json={})
    second = _create(estate, rows=3)
    _ok(estate, "/v1/table/db$t/drop", json={})

    asked = estate.client.post("/management/v1/table/db$t/undrop", headers=estate.bearer("alice"))
    restored = _ok(estate, f"/management/v1/table/db$t/undrop?uuid={first}")

    assert (asked.status_code, first in asked.text, second in asked.text) == (409, True, True), asked.text
    assert restored.status_code == 200
    assert _ok(estate, "/v1/table/db$t/describe", json={}).json()["properties"]["rask.uuid"] == first
    assert int(_ok(estate, "/v1/table/db$t/count_rows", json={}).text) == 2
    assert estate.client.post("/v1/table/db$t/describe", json={}, headers=estate.bearer("bob")).status_code == 200, "the grant did not come back"


@pytest.mark.parametrize("held", ["the-table's-name", "its-namespace's-name"])
def test_an_undrop_while_another_object_holds_its_name_or_its_namespaces_is_refused(estate: Any, held: str) -> None:  # noqa: ANN401
    _create(estate, rows=2)
    if held == "the-table's-name":
        _ok(estate, "/v1/table/db$t/drop", json={})
        _create(estate, rows=3)
    else:
        _ok(estate, "/v1/namespace/db/drop", json={"behavior": "Cascade"})
        _ok(estate, "/v1/namespace/db/create", json={})
    standing = estate.store.tuples()

    refused = estate.client.post("/management/v1/table/db$t/undrop", headers=estate.bearer("alice"))

    assert refused.status_code == 409, refused.text
    assert estate.store.tuples() == standing, "a refused undrop changed a grant"
