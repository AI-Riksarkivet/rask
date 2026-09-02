"""Three small spec deviations: the shipped client's HTTP method, the stubs' status codes, and `on`.

**A3 — GET vs POST.** The spec says POST for `count_rows` and `tags/list` at every tag from v0.9.0 to
v0.12.0, and lance-namespace's own generated reqwest client sends POST. But the REST client pylance
BUNDLES from the lance repository (`rust/lance-namespace-impls/src/rest.rs`) calls `get_json` for
exactly those two ops, and the reference SERVER it bundles mounts them as GET — so the lance repo
disagrees with its own spec on both sides of the wire, and `lance_namespace.connect("rest", …)`
resolves to that class. rask mounted POST only, so the client got FastAPI's default 405
`{"detail": "Method Not Allowed"}`, which carries no `code` and surfaces as `InternalError 18`.
Dual-mounting is the local fix; the upstream fix is a one-line change in lance, worth filing.

**A8 — stub status codes.** `CreateMaterializedView` is 201 in the spec, `RefreshMaterializedView`
and `AlterTableBackfillColumns` are 202. All three carry no `status_code` on their decorator, so a
future backend would silently answer 200. They answer a spec-correct 501 today, which is why this has
never bitten — it is latent drift, fixed now while it is one line each.

**A9 — `on` is an array, and it is BLOCKED ON A10.** lance-namespace 0.12.0 makes
`MergeInsertIntoTableRequest.on` a list of field paths sent as a repeated query parameter. Widening
only the door was tried and reversed: the installed 0.11.0 model types `on` as `str` with `MinLen(1)`,
so a list fails validation inside the request model rather than at the door — a 0.12.0 client would
get a pydantic error one layer deeper, which reads as a rask bug rather than a version skew. The case
below pins the current single-key contract so the change lands WITH the bump.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import ListTableTagsResponse, MergeInsertIntoTableResponse


ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}


# --- A3 -------------------------------------------------------------------------------------------


def test_count_rows_is_reachable_by_GET(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.count_table_rows.return_value = 3
    resp = client.get("/v1/table/db$t/count_rows")
    assert resp.status_code == 200, f"the bundled client sends GET; got {resp.status_code} {resp.text[:120]}"
    assert resp.json() == 3


def test_tags_list_is_reachable_by_GET(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """`tags/list` reads the tag store off a dataset handle, so mocking the NAMESPACE is not enough —
    the dataplane call is what has to be stubbed for a method-routing test."""
    from catalog.api.v1.endpoints import tags as tags_module

    monkeypatch.setattr(tags_module.dataplane, "list_tags", lambda *a, **k: ListTableTagsResponse(tags={}))
    resp = client.get("/v1/table/db$t/tags/list")
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:120]}"


@pytest.mark.parametrize("path", ["/v1/table/db$t/count_rows", "/v1/table/db$t/tags/list"])
def test_the_POST_form_still_works(client: TestClient, fake_ns: MagicMock, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    """The spec's own method must keep working — dual-mount, not a move."""
    from catalog.api.v1.endpoints import tags as tags_module

    fake_ns.count_table_rows.return_value = 3
    monkeypatch.setattr(tags_module.dataplane, "list_tags", lambda *a, **k: ListTableTagsResponse(tags={}))
    assert client.post(path, json={}).status_code == 200


# --- A8 -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "spec_status"),
    [
        ("/v1/materialized_view/db$v/create", 201),
        ("/v1/materialized_view/db$v/refresh", 202),
        ("/v1/table/db$t/backfill_column", 202),
    ],
)
def test_the_stub_routes_declare_the_spec_status(client: TestClient, path: str, spec_status: int) -> None:
    """Declared on the decorator, so a future backend cannot silently answer 200.

    Asserted through the OpenAPI rather than by calling: all three answer a spec-correct 501 today
    (the `dir` backend stubs them), so a live call can never exercise the success status.
    """
    served = client.app.openapi()["paths"][path.replace("db$v", "{id}").replace("db$t", "{id}")]["post"]["responses"]
    assert str(spec_status) in served, f"{path} declares {sorted(served)}, not {spec_status}"


# --- A9 — blocked on A10, and the block is the point --------------------------------------------


def test_merge_insert_takes_a_single_on_until_the_namespace_bump(client: TestClient, fake_ns: MagicMock) -> None:
    """`on` is a single key today, and cannot become an array before A10.

    lance-namespace 0.12.0 makes `MergeInsertIntoTableRequest.on` a list of field paths (a composite
    merge key) sent as a repeated query parameter. The INSTALLED 0.11.0 model types it `str` with
    `MinLen(1)`, so widening only the door pushes the failure one layer deeper — a 0.12.0 client would
    get a pydantic ValidationError out of the request model instead of a clean answer, which reads as a
    rask bug rather than a version skew. Measured: that is exactly what happened when the door was
    widened first.

    This pins the current contract so the change lands with the bump, not before it.
    """
    fake_ns.merge_insert_into_table.return_value = MergeInsertIntoTableResponse(version=2)
    assert client.post("/v1/table/db$t/merge_insert?on=id", content=b"A", headers=ARROW_STREAM).status_code == 200
    assert fake_ns.merge_insert_into_table.call_args.args[0].on == "id"
