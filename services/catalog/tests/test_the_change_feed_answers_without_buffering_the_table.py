"""The change-feed ROUTE hands its answer out progressively, not the service function alone.

`dataplane.read_changes` yielding is necessary and not sufficient: a route that wraps a generator in
`Response(content=...)` re-buffers the whole thing and puts every byte back in the pod, with the
service-level test still green. So this drives the door through the app and asserts the property that
only the wire can show — the response carries no `Content-Length`, because FastAPI cannot know one
without consuming the generator first.

The second assertion is that streaming did not cost the contract: the body is still a complete Arrow
FILE (`open_file` validates the footer, which arrives as the last chunk) carrying every row.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import lance
import pyarrow as pa
import pyarrow.ipc
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace, get_settings, get_storage_options
from catalog.api.v1.endpoints import data as door
from catalog.core.config import Settings
from service_kit.lakehouse.ns_errors import install_problem_handlers


#: Enough rows that the scan spans several batches — one batch would make "it streamed" unfalsifiable.
_ROWS = 50_000


@pytest.fixture
def table_uri(tmp_path: Any) -> str:
    uri = str(tmp_path / "t")
    lance.write_dataset(
        pa.table({"id": pa.array(range(_ROWS), pa.int64())}),
        uri,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    return uri


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, table_uri: str) -> Iterator[TestClient]:
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s")
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.management_router)
    application.state.dapr_client = None

    monkeypatch.setattr(door.dataplane, "open_dataset", lambda *_a, **_kw: lance.dataset(table_uri))
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: object()
    application.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(application) as test_client:
        yield test_client


def test_the_route_does_not_declare_a_length_it_would_have_to_buffer_to_know(client: TestClient) -> None:
    """A `Content-Length` on this route means the whole answer was assembled before the first byte
    left — which is the defect, stated in a header."""
    response = client.post("/management/v1/table/t/changes", json={"begin_version": 0, "kind": "inserted"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == door.ARROW_FILE
    assert "content-length" not in response.headers, "the route buffered its answer to measure it before sending"


def test_streaming_did_not_cost_the_ARROW_FILE_contract(client: TestClient) -> None:
    """The footer is written when the IPC writer closes, so it is the LAST thing yielded. A generator
    abandoned early, or a route that stops reading at the last batch, produces a body that looks
    complete and fails to open — which is why this asserts through `open_file` rather than on length."""
    response = client.post("/management/v1/table/t/changes", json={"begin_version": 0, "kind": "inserted"})

    table = pyarrow.ipc.open_file(pa.py_buffer(response.content)).read_all()

    assert table.num_rows == _ROWS, f"the feed answered {table.num_rows} of {_ROWS} rows"
    assert "_row_created_at_version" in table.schema.names, "the checkpoint column a consumer advances on did not survive streaming"


def test_a_COLUMN_THE_TABLE_DOES_NOT_HAVE_is_a_4xx_and_not_a_truncated_200(client: TestClient) -> None:
    """The reason the first batch is pulled inside `_user_sql` rather than inside the generator.

    MEASURED on the installed pylance: `dataset.scanner(columns=["not_a_column"])` CONSTRUCTS without
    complaint and raises `ValueError: Schema error: No field named not_a_column` only when something
    pulls a batch. `columns` is caller-supplied on this route, so that is a reachable request — and a
    generator's body does not run until the response has already begun. Pulled late it would be a 200
    carrying a few bytes that never open; pulled inside the guard it is a refusal the caller can act on.

    An inverted version window is deliberately NOT the case under test here: `changes.change_filter`
    refuses that before the scan is built, so it would pass with or without the guard and prove
    nothing about where the pull happens.
    """
    response = client.post(
        "/management/v1/table/t/changes",
        json={"begin_version": 0, "kind": "inserted", "columns": ["not_a_column"]},
    )

    assert 400 <= response.status_code < 500, f"an unknown column answered {response.status_code}, not a client error"


def test_the_stream_survives_the_MIDDLEWARE_THE_SERVICE_ACTUALLY_SHIPS(table_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture above builds a bare app; production does not.

    Middleware is where a streaming response quietly stops streaming, and the endpoint test above
    would stay green through it while the pod paid exactly what it paid before. `main.py` adds two:
    `BodySizeLimitMiddleware` and `WriteConcurrencyLimitMiddleware`.

    WHAT THE ASSERTION CATCHES, measured rather than reasoned: a middleware that drains
    `response.body_iterator` and returns a plain `Response` sets `Content-Length` (32 on a two-chunk
    probe), so its absence is real evidence the bytes were never collected. Deriving from
    `BaseHTTPMiddleware` is NOT by itself the hazard — a pass-through one was driven here and streams
    with no `Content-Length`; it is COLLECTING the body that does it, whatever the base class.

    Driving the real pair rather than reading their definitions, because which middleware the service
    mounts is the kind of fact that changes without this route noticing.
    """
    from catalog.api.load_shed import WriteConcurrencyLimitMiddleware
    from service_kit.body_limit import BodySizeLimitMiddleware

    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s")
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.management_router)
    application.state.dapr_client = None
    application.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    application.add_middleware(WriteConcurrencyLimitMiddleware, max_concurrent=settings.max_concurrent_writes)

    monkeypatch.setattr(door.dataplane, "open_dataset", lambda *_a, **_kw: lance.dataset(table_uri))
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: object()
    application.dependency_overrides[get_storage_options] = lambda: {}

    with TestClient(application) as client:
        response = client.post("/management/v1/table/t/changes", json={"begin_version": 0, "kind": "inserted"})

    assert response.status_code == 200, response.text
    assert "content-length" not in response.headers, "a middleware buffered the stream to measure it"
    assert pyarrow.ipc.open_file(pa.py_buffer(response.content)).read_all().num_rows == _ROWS
