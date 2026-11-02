"""The concrete CatalogPublisher — the transport half of the publish saga.

`Publisher` is a Protocol precisely so the saga's orchestration tests need no catalog; these are the
tests for the OTHER side of that seam. Driven over a real socket at a create door that records each
request, because the publisher speaks through the stock Lance Namespace SDK, whose REST client is Rust:
what the catalog receives is observable only at a server. What is pinned is the CONTRACT this class
holds up to the saga:

- create posts `mode=exist_ok` with the plan's rows as an Arrow-IPC stream typed by the published
  schema, and the properties JSON-encoded.
- every call presents the annotator's own projected ServiceAccount token, read again for that call
  because the kubelet replaces it before it expires, and names the person the publish is for on
  `x-lance-originator`: the service authors every event the publish produces, so the header is the
  only place the human survives (ADR 0171).
- a create the catalog does not take is sent again, which `mode=exist_ok` makes safe, and only
  `CREATE_ATTEMPTS` times.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from annotator.projects import lakehouse
from annotator.projects.lakehouse import CatalogPublisher
from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA, PublishPlan
from service_kit.exceptions import ServiceUnavailableError


def _plan(rows: int = 1) -> PublishPlan:
    return PublishPlan(
        project_id="p1",
        publish_id="0123456789abcdef",
        rows=[
            dict.fromkeys(PUBLISHED_LABELS_SCHEMA.names)
            | {"project_id": "p1", "task_id": f"t{i}", "shape_type": "bbox", "polygon": [], "attributes": "{}", "difficult": False}
            for i in range(rows)
        ],
    )


def _token(path: Path, value: str) -> Path:
    path.write_text(value)
    return path


class _Call(BaseModel):
    """What one create request carried to the catalog, its header names lower-cased."""

    table_id: str
    params: dict[str, str]
    headers: dict[str, str]
    body: bytes


class _Catalog:
    """A catalog whose create door records each request and answers ``version``, after refusing the first
    ``unavailable`` requests with the spec's 503 code 17 (ServiceUnavailable)."""

    def __init__(self) -> None:
        self.url = ""
        self.calls: list[_Call] = []
        self.version = 7
        self.unavailable = 0


@pytest.fixture
def catalog() -> Iterator[_Catalog]:
    door = _Catalog()
    app = FastAPI()

    @app.post("/v1/table/{table_id}/create")
    async def create(table_id: str, request: Request) -> JSONResponse:
        door.calls.append(_Call(table_id=table_id, params=dict(request.query_params), headers=dict(request.headers), body=await request.body()))
        if door.unavailable:
            door.unavailable -= 1
            return JSONResponse({"error": "the catalog is not taking writes", "code": 17}, status_code=503)
        return JSONResponse({"version": door.version})

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    door.url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    yield door
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.asyncio
async def test_create_posts_exist_ok_with_schema_typed_rows_and_its_run(catalog: _Catalog, tmp_path: Path) -> None:
    catalog.version = 9
    publisher = CatalogPublisher(catalog.url, token_file=_token(tmp_path / "token", "svc-token"), originator="alice")

    version = await publisher.create_table("silver$vasa_0123456789ab", _plan(rows=2), properties={"annotation.project_id": "p1"}, run_id="pub-1")

    [call] = catalog.calls
    assert (version, call.table_id) == (9, "silver$vasa_0123456789ab")
    assert call.params["mode"] == "exist_ok", "the saga's retry safety RELIES on exist_ok — any other mode re-creates or errors on replay"
    assert json.loads(call.params["properties"]) == {"annotation.project_id": "p1"}
    assert (call.headers["x-rask-run-id"], "x-rask-inputs" in call.headers) == ("pub-1", False), "an absent pin names no input (§7.2 — never fabricate)"
    # The service's bearer authors the events, so the originator header is the only place the human survives.
    assert (call.headers["authorization"], call.headers["x-lance-originator"]) == ("Bearer svc-token", "alice")
    # A readable Arrow-IPC STREAM carrying the published schema: an IPC file, or inferred types, would fail the
    # catalog's reader or type an all-sentinel publish as nulls.
    table = pa.ipc.open_stream(call.body).read_all()
    assert (table.schema.names, table.num_rows) == (list(PUBLISHED_LABELS_SCHEMA.names), 2)


@pytest.mark.asyncio
async def test_the_pin_reaches_the_create_as_the_runs_input(catalog: _Catalog) -> None:
    """§7.2 over the wire: the one pinned source is the run's input in the catalog Context."""
    await CatalogPublisher(catalog.url).create_table("silver$t_1", _plan(), properties={}, run_id="pub-1", source="demo$pages", source_version=24)

    assert json.loads(catalog.calls[0].headers["x-rask-inputs"]) == [{"table": ["demo", "pages"], "version": 24}]


@pytest.mark.asyncio
async def test_every_publish_call_presents_the_annotators_own_token_read_again_and_names_the_person(catalog: _Catalog, tmp_path: Path) -> None:
    """The token as it is when each call is made, and the person the publish is for.

    The kubelet replaces the projected token before each one expires, so a token read once at construction would present
    an expired credential to every call after the first.
    """
    token = _token(tmp_path / "token", "token-1")
    publisher = CatalogPublisher(catalog.url, token_file=token, originator="dex~gina")

    await publisher.create_table("acme-silver$labels_p1", _plan(), properties={}, run_id="pub-1")
    _token(token, "token-2")
    await publisher.create_table("acme-silver$labels_p1", _plan(), properties={}, run_id="pub-1")

    presented = [(call.headers["authorization"], call.headers["x-lance-originator"]) for call in catalog.calls]
    assert presented == [("Bearer token-1", "dex~gina"), ("Bearer token-2", "dex~gina")]


@pytest.mark.asyncio
async def test_a_create_the_catalog_does_not_take_is_sent_again(catalog: _Catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    """A create answered 503 is sent again rather than failing the publish until the watchdog's next tick: pylance's REST
    client retries nothing itself."""
    monkeypatch.setattr(lakehouse, "CREATE_BACKOFF_S", 0.0)
    catalog.unavailable = lakehouse.CREATE_ATTEMPTS - 1

    version = await CatalogPublisher(catalog.url).create_table("silver$t_1", _plan(), properties={}, run_id="pub-1")

    assert (version, len(catalog.calls)) == (7, lakehouse.CREATE_ATTEMPTS)


@pytest.mark.asyncio
async def test_a_create_the_catalog_never_takes_fails_after_its_attempts(catalog: _Catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    """The attempts are bounded, and spending them fails the saga run with the catalog's unavailability rather than
    reporting a version the catalog never committed."""
    monkeypatch.setattr(lakehouse, "CREATE_BACKOFF_S", 0.0)
    catalog.unavailable = lakehouse.CREATE_ATTEMPTS

    with pytest.raises(ServiceUnavailableError, match="the catalog is unavailable"):
        await CatalogPublisher(catalog.url).create_table("silver$t_1", _plan(), properties={}, run_id="pub-1")
    assert len(catalog.calls) == lakehouse.CREATE_ATTEMPTS
