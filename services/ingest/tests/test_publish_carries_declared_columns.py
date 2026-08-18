"""The publish door runs the breaking-change detector only if the caller declares its columns.

`assert_quality` adds one `column_declared` assertion per declared name — the check that a version
which dropped or renamed a column a consumer depends on is refused BEFORE it is published, rather than
failing later inside that consumer's job. `PublishRequest` accepts `required_columns`; no caller sent
any, so the door has only ever run two assertions. Measured on a table missing a declared column:

    no required_columns : 2 assertions  [row_count_positive, not_null]        -> PASSES
    declared            : 5 assertions  [.., column_declared x3]              -> REFUSED on `thumbnail`

This matters most for a change not yet made. The medallion movers carry `requiredColumns` in the chart
and run the identical assertions locally today; the design deletes that local gate once movers publish,
on the grounds that the catalog runs "the identical assertions at the identical seam". It does not —
not without this — and deleting the mover's gate first would retire the detector silently and turn
`requiredColumns` into dead config.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from ingest.catalog_service import CatalogServiceClient


CATALOG = "http://catalog.test"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> CatalogServiceClient:
    import pyarrow as pa

    monkeypatch.delenv("RASK_CATALOG_SERVICE_IDENTITY", raising=False)
    monkeypatch.delenv("RASK_CATALOG_APP_TOKEN", raising=False)
    return CatalogServiceClient(pa.schema([pa.field("id", pa.int64())]), base_url=CATALOG, token="t")


def _route() -> respx.Route:
    return respx.post(f"{CATALOG}/v1/table/bronze$pages/publish").mock(
        return_value=httpx.Response(200, json={"published": True, "from_version": 1, "to_version": 2})
    )


def _sent(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)


class TestTheDeclaredColumnsTravel:
    @respx.mock
    def test_they_reach_the_door(self, client: CatalogServiceClient) -> None:
        route = _route()

        client.publish("bronze", "pages", 2, key_column="id", required_columns=("id", "thumbnail"))

        assert _sent(route)["required_columns"] == ["id", "thumbnail"]

    @respx.mock
    def test_the_version_and_key_column_are_unchanged(self, client: CatalogServiceClient) -> None:
        route = _route()

        client.publish("bronze", "pages", 2, key_column="page_id", required_columns=("id",))

        sent = _sent(route)
        assert sent["version"] == 2
        assert sent["key_column"] == "page_id"


class TestDeclaringNothing:
    @respx.mock
    def test_an_empty_declaration_sends_an_empty_list_not_a_missing_key(self, client: CatalogServiceClient) -> None:
        """`PublishRequest` defaults it to `[]`, so either shape works on the wire — sending it
        explicitly keeps the request a full statement of what was asked for rather than a partial one."""
        route = _route()

        client.publish("bronze", "pages", 2, key_column="id")

        assert _sent(route)["required_columns"] == []

    @respx.mock
    def test_a_caller_that_declares_nothing_still_publishes(self, client: CatalogServiceClient) -> None:
        _route()

        assert client.publish("bronze", "pages", 2, key_column="id")["published"] is True
