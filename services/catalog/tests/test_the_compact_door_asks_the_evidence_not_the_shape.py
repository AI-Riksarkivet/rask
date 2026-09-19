"""`maintenance/compact` answers a branch from EVIDENCE about the dataset, not from its shape.

[[LH-019]]. The door carried its own refusal in front of `require_compactable`, and the two disagreed
about why. The door said "a rewrite here would materialise the parent's bytes into `tree/<branch>/`
rather than merge this ref's own fragments". Measured against pylance 11.0.0 — a branch with 3
inherited and 2 of its own fragments, compacted:

    CompactionMetrics(fragments_removed=5, fragments_added=1, files_removed=5, files_added=1)
    main data files: 3 before, 3 after, byte-identical
    branch 40 rows, main 30 rows, both still reading

So it DOES merge this ref's own fragments, and the parent is not endangered. The real cost is
duplication — the branch stops being a shallow clone — which is precisely what `require_compactable`
already measures and refuses on ("compacting one materialises the shared data into its own root,
1,072 -> 108,199 bytes against a 119,693-byte base"). A branch IS a shallow clone
(`lance_docs/file_format.md:2744`), so it reaches that gate and gets that answer.

THE SHAPE REFUSAL ALSO MADE THE ANSWER PERMANENT. A branch that has already been materialised owns
every fragment it reads and is as cheap to compact as any table; a gate that refuses on the shape of
the request can never notice that, and this is the same defect `require_compactable` itself closed one
level down — "this door was the STRICTER of the two while its refusal told the operator the sweep
agreed with it".
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


_URI = "s3://warehouse/aa3bed10_ns$events"


class _Opened:
    """Records the ref each open was asked for — the assertion that matters for a door that used to refuse."""

    def __init__(self) -> None:
        self.refs: list[str | None] = []


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> _Opened:
    seen = _Opened()

    class _Ds:
        uri = _URI

    def _open(ns: object, so: object, segments: object, **kwargs: Any) -> _Ds:
        seen.refs.append(kwargs.get("branch"))
        return _Ds()

    monkeypatch.setattr(door, "open_dataset", _open)
    return seen


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, opened: _Opened) -> Iterator[TestClient]:
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s")
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.state.dapr_client = None

    monkeypatch.setattr(door, "_base_refs", _no_refs)
    monkeypatch.setattr(door.maintenance, "compact_now", lambda ds, **kwargs: {"ok": True, "fragments_removed": 4, "fragments_added": 1})
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: object()
    application.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(application) as test_client:
        yield test_client


async def _no_refs(ds: object, so: object) -> Any:
    from service_kit.lakehouse.base_refs import BaseRefs

    return BaseRefs()


def test_a_branch_is_no_longer_refused_on_its_shape(client: TestClient) -> None:
    response = client.post("/v1/table/ns$events/maintenance/compact?branch=work", json={})

    assert response.status_code == 200, response.text


def test_the_door_opens_the_REF_the_request_names(client: TestClient, opened: _Opened) -> None:
    """A 200 says the door accepted; only the ref it opened says it acted on the branch.

    This is the wrong-but-plausible answer the row exists to remove — compacting main and reporting it
    as the branch's is worse than the refusal was, because nothing downstream can tell.
    """
    client.post("/v1/table/ns$events/maintenance/compact?branch=work", json={})

    assert opened.refs == ["work"]


def test_a_branchless_request_still_opens_main(client: TestClient, opened: _Opened) -> None:
    """The control. Without it, a door stamping every open with a branch would pass above."""
    client.post("/v1/table/ns$events/maintenance/compact", json={})

    assert opened.refs == [None]


def test_the_wire_contract_no_longer_advertises_a_refusal(client: TestClient) -> None:
    """The description is generated into the typed client, so a stale "REFUSED here" is a lie on the wire."""
    schema = client.get("/openapi.json")
    if schema.status_code != 200:
        pytest.skip("this app mounts no openapi route")
    params = schema.json()["paths"]["/v1/table/{id}/maintenance/compact"]["post"]["parameters"]
    description = next(p["description"] for p in params if p["name"] == "branch")

    assert "REFUSED" not in description
