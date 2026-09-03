"""An enqueued maintenance unit names the TABLE it is for, not only the path it lives at.

The executor must be able to ask the catalog for a credential scoped to the dataset it is about to
rewrite, and the catalog is addressed by IDENTIFIER — `POST /v1/table/{id}/credentials` — never by
location. So a unit carrying only a URI can be executed, but cannot be executed with a scoped
credential; it can only be signed by whatever ambient key the process holds.

Recovering the id from the path does not work, and that was measured rather than assumed. Of eleven
top-level roots in the live warehouse, `table_id_from_uri` recovers an id from six: it reads the flat
`<uuid8>_<table_id>` layout and returns None for everything else. The five it cannot read include
`medallion/`, the cascade — the highest-churn writer in the estate. Those five are NOT unknown to the
catalog, which is the whole point: `bronze$events` (at `s3://lance-catalog/medallion/bronze`) and
`bronze$pages` (at `s3://lance-catalog/bronze/pages`) both answer a write-tier vend with 200. The
identity exists; only the parser cannot see it in the path.

The producer, however, always can. This door has the id in its own request path. So it stamps it, and
the derivation stops being the only route.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace, get_settings, get_storage_options
from catalog.api.v1.endpoints import maintenance as door
from catalog.core.config import Settings
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.lakehouse.work_items import DatasetWorkItem


class _Published:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish_event(self, publisher: object, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> _Published:
    recorder = _Published()
    monkeypatch.setattr(door.dapr_publish, "publish_event", recorder.publish_event)
    return recorder


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_MAINTENANCE_WORK_TOPIC="maintenance.work.v1")
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.state.dapr_client = object()

    class _Ds:
        # The MEDALLION layout on purpose: this is the URI shape `table_id_from_uri` cannot read, so a
        # test using the flat layout would pass with the identity still coming from the path.
        uri = "s3://lance-catalog/medallion/bronze"

    monkeypatch.setattr(door, "open_dataset", lambda ns, so, segments: _Ds())
    monkeypatch.setattr(door.base_refs, "sibling_base_refs", lambda uri, so: door.base_refs.BaseRefs())
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: object()
    application.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(application) as c:
        yield c


def test_the_unit_names_the_table_the_door_was_called_on(client: TestClient, published: _Published) -> None:
    client.post("/v1/table/bronze%24events/maintenance/compact", json={})
    item = DatasetWorkItem.model_validate_json(published.calls[0]["data"])
    assert item.table_id == "bronze$events", "the unit carries no table identity, so the executor can only sign this rewrite with the ambient credential"


def test_the_identity_survives_a_uri_no_parser_can_read(client: TestClient, published: _Published) -> None:
    """`s3://lance-catalog/medallion/bronze` yields no id to `table_id_from_uri`. The door still knows."""
    from maintenance.core.lineage_emit import table_id_from_uri

    client.post("/v1/table/bronze%24events/maintenance/compact", json={})
    item = DatasetWorkItem.model_validate_json(published.calls[0]["data"])
    assert table_id_from_uri(item.uri) is None, "pick a URI the parser genuinely cannot read, or this proves nothing"
    assert item.table_id == "bronze$events"


def test_a_unit_without_an_identity_is_still_valid(client: TestClient) -> None:
    """The sweep starts from a bare URI and may have no id. That must remain expressible rather than
    forcing a producer to invent one — an invented id would vend a credential for the wrong table."""
    item = DatasetWorkItem(uri="s3://b/unknown", plan=DatasetWorkItem.model_fields["plan"].annotation())
    assert item.table_id is None
