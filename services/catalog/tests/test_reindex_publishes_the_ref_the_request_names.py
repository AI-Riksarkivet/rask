"""`maintenance/reindex` accepts a branch, and the unit it publishes CARRIES it.

[[LH-019]]. This was the last of the three ungated branch doors, and its refusal was correct while
`IndexWorkItem` had no `branch`: the door publishes and answers 202, so the WORKER opens the dataset.
A door accepting a branch it could not carry would have main's index rebuilt while the API reported
the branch's — worse than a refusal, because nothing downstream can tell. `IndexWorkItem.branch` and
the worker's `checkout_version((branch, None))` close that, so refusing is now the wrong answer.

ASSERTED ON THE PUBLISHED UNIT, never on the status code. A 202 says the door accepted the request
and says nothing about which ref the build happens on, which is exactly the gap that made the
refusal right in the first place.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace, get_settings, get_storage_options
from catalog.api.v1.endpoints import maintenance as door
from catalog.core.config import Settings
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.lakehouse.work_items import IndexWorkItem


_URI = "s3://warehouse/aa3bed10_ns$events"


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
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_MAINTENANCE_INDEX_TOPIC="maintenance.index.v1")
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.state.dapr_client = object()

    class _Ds:
        uri = _URI

    class _Spec:
        column, kind, index_type, name, params = "vec", "vector", "IVF_PQ", "vec_idx", {}

    monkeypatch.setattr(door, "open_dataset", lambda ns, so, segments, **kwargs: _Ds())
    monkeypatch.setattr(door.index_specs, "describe_index_for_rebuild", lambda ds, name: _Spec())
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: object()
    application.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(application) as test_client:
        yield test_client


def _unit(published: _Published) -> IndexWorkItem:
    assert published.calls, "the door published nothing, so there is no unit whose ref to check"
    return IndexWorkItem.model_validate_json(published.calls[-1]["data"])


def test_the_door_no_longer_refuses_a_branch(client: TestClient, published: _Published) -> None:
    response = client.post("/management/v1/table/ns$events/maintenance/reindex?branch=work", json={"index_name": "vec_idx"})

    assert response.status_code == 202, response.text


def test_the_published_unit_names_the_branch(client: TestClient, published: _Published) -> None:
    """The assertion that matters: a 202 whose unit says main is the wrong-but-plausible answer."""
    client.post("/management/v1/table/ns$events/maintenance/reindex?branch=work", json={"index_name": "vec_idx"})

    assert _unit(published).branch == "work"


def test_a_branchless_request_still_publishes_a_unit_for_main(client: TestClient, published: _Published) -> None:
    """The control. Without it, a door stamping every unit with a branch would pass above."""
    client.post("/management/v1/table/ns$events/maintenance/reindex", json={"index_name": "vec_idx"})

    assert _unit(published).branch == ""


def test_the_two_refs_publish_two_units(client: TestClient, published: _Published) -> None:
    """The caller follows `transaction_id`, so one id for two builds points them at the wrong one."""
    client.post("/management/v1/table/ns$events/maintenance/reindex?branch=work", json={"index_name": "vec_idx"})
    on_branch = _unit(published).unit_id
    client.post("/management/v1/table/ns$events/maintenance/reindex", json={"index_name": "vec_idx"})

    assert on_branch != _unit(published).unit_id
