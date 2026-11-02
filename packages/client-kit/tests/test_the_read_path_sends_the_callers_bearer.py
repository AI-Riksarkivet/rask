"""A client's catalog read path carries the CALLER's identity, and never a service token in its place.

Why the CALLER's bearer and not a service account: the catalog checks one relation on one `table:` object and injects no
row predicate, so a service credential answers 200 for a user who has no grant: the two principals diverge, not the rows.
That is a confused deputy, and it is why the estate's one service account is scoped to the publish saga, whose stated
reason ("outlives any user request") does not apply to a request-scoped read. A caller with no bearer sends none, so an
anonymous request is never re-issued under the estate's own identity.

Driven over a real socket, because the read path speaks through the stock Lance Namespace SDK, whose REST client is Rust:
what the catalog receives is observable only at a server.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Annotated

import pytest
import uvicorn
from fastapi import FastAPI, Header

from client_kit.lancekit.reader import open_reader


class _Settings:
    """The `TablePathSettings` shape in full catalog mode, with a configurable service token."""

    read_backend = "catalog"
    write_backend = "catalog"
    rest_catalog_mode = True
    catalog_delimiter = "$"

    def __init__(self, catalog_uri: str, service_token: str | None) -> None:
        self.catalog_uri = catalog_uri
        self.catalog_token = service_token


class _Catalog:
    """A catalog's describe door that records the table each request names and the bearer it carries."""

    def __init__(self, url: str, calls: list[tuple[str, str | None]]) -> None:
        self.url = url
        self.calls = calls


@pytest.fixture
def catalog() -> Iterator[_Catalog]:
    calls: list[tuple[str, str | None]] = []
    app = FastAPI()

    @app.post("/v1/table/{table_id}/describe")
    def describe(table_id: str, authorization: Annotated[str | None, Header()] = None) -> dict[str, object]:
        calls.append((table_id, authorization))
        return {"version": 7}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    yield _Catalog(f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}", calls)
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.parametrize(
    ("service_token", "caller_token", "expected"),
    [
        pytest.param(None, "caller-jwt", "Bearer caller-jwt", id="the-caller-bearer-travels"),
        pytest.param("service-jwt", None, None, id="no-caller-no-bearer"),
    ],
)
def test_the_reader_sends_the_callers_bearer_and_never_a_service_token(
    catalog: _Catalog, service_token: str | None, caller_token: str | None, expected: str | None
) -> None:
    reader = open_reader(dataset=None, table_id=["transcripts_v2", "annotations"], settings=_Settings(catalog.url, service_token), caller_token=caller_token)

    version = reader.table_version()

    assert (version, catalog.calls) == (7, [("transcripts_v2$annotations", expected)])
