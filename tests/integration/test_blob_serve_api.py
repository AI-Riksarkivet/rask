"""§9 P1 blob serving over HTTP — GET /v1/table/{id}/blobs end to end.

Unlike the sibling integration suites (MagicMock namespace), these run a REAL ``dir`` namespace +
real pylance behind the endpoint: the value under test is the HTTP contract — status codes
(200/206/416/400/404), the Range/Content-Range/Accept-Ranges headers, and the problem+json error
mapping — on top of genuine blob reads, exactly what a credential-less consumer sees.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance import blob_array, blob_field
from lance_namespace import connect


def _blob_ipc(payloads: list[bytes | None]) -> bytes:
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("src", pa.string())])
    table = pa.table(
        {"id": list(range(len(payloads))), "payload": blob_array(payloads), "src": ["cam"] * len(payloads)},
        schema=schema,
    )
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture
def blob_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient over the app with a REAL dir namespace rooted in tmp_path, pre-seeded with a
    three-row blob table (``clips``: ``b"hello-world"``, 100 ``X`` bytes, and a zero-length payload)."""
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(tmp_path))
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")

    from catalog.core.config import get_settings

    get_settings.cache_clear()

    from catalog.api.dependencies import get_namespace, get_storage_options
    from catalog.main import app
    from catalog.services.dataplane import create_table

    ns = connect("dir", {"root": str(tmp_path)})
    create_table(ns, {}, ["clips"], _blob_ipc([b"hello-world", b"X" * 100, b""]), mode="create")

    app.dependency_overrides[get_namespace] = lambda: ns
    app.dependency_overrides[get_storage_options] = lambda: {}
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_get_blob_full_200(blob_client: TestClient) -> None:
    resp = blob_client.get("/v1/table/clips/blobs", params={"column": "payload", "row": 0})
    assert resp.status_code == 200
    assert resp.content == b"hello-world"
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == "11"
    assert resp.headers["etag"] == '"1-payload-0"'  # strong validator for safe resumption
    assert "content-range" not in resp.headers


def test_get_blob_range_206_with_content_range(blob_client: TestClient) -> None:
    resp = blob_client.get(
        "/v1/table/clips/blobs",
        params={"column": "payload", "row": 0},
        headers={"Range": "bytes=0-3"},
    )
    assert resp.status_code == 206
    assert resp.content == b"hell"
    assert resp.headers["content-range"] == "bytes 0-3/11"
    assert resp.headers["accept-ranges"] == "bytes"


def test_get_blob_suffix_range_206(blob_client: TestClient) -> None:
    resp = blob_client.get(
        "/v1/table/clips/blobs",
        params={"column": "payload", "row": 0},
        headers={"Range": "bytes=-5"},
    )
    assert resp.status_code == 206
    assert resp.content == b"world"
    assert resp.headers["content-range"] == "bytes 6-10/11"


def test_get_blob_unsatisfiable_range_416(blob_client: TestClient) -> None:
    resp = blob_client.get(
        "/v1/table/clips/blobs",
        params={"column": "payload", "row": 0},
        headers={"Range": "bytes=999-"},
    )
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */11"
    assert resp.content == b""


def test_get_blob_malformed_range_ignored_full_200(blob_client: TestClient) -> None:
    # RFC 9110 §14.1.1: a server MAY ignore a Range it doesn't support — multi-range falls back to 200.
    resp = blob_client.get(
        "/v1/table/clips/blobs",
        params={"column": "payload", "row": 0},
        headers={"Range": "bytes=0-3,6-9"},
    )
    assert resp.status_code == 200
    assert resp.content == b"hello-world"


def test_get_blob_non_blob_column_400(blob_client: TestClient) -> None:
    resp = blob_client.get("/v1/table/clips/blobs", params={"column": "src", "row": 0})
    assert resp.status_code == 400
    assert "not a blob column" in resp.json()["detail"]


def test_get_blob_unknown_column_404(blob_client: TestClient) -> None:
    resp = blob_client.get("/v1/table/clips/blobs", params={"column": "nope", "row": 0})
    assert resp.status_code == 404
    assert resp.json()["title"] == "TableColumnNotFoundError"


def test_get_blob_row_out_of_range_400(blob_client: TestClient) -> None:
    resp = blob_client.get("/v1/table/clips/blobs", params={"column": "payload", "row": 99})
    assert resp.status_code == 400
    assert "out of range" in resp.json()["detail"]


def test_get_blob_missing_table_404(blob_client: TestClient) -> None:
    resp = blob_client.get("/v1/table/ghost/blobs", params={"column": "payload", "row": 0})
    assert resp.status_code == 404


def test_get_blob_negative_row_422(blob_client: TestClient) -> None:
    resp = blob_client.get("/v1/table/clips/blobs", params={"column": "payload", "row": -1})
    assert resp.status_code == 422


def test_get_blob_zero_length_payload_200_empty(blob_client: TestClient) -> None:
    # A zero-length (or null — stored identically at pylance 8.0.0) payload is valid empty bytes,
    # not an error; any Range against it is unsatisfiable.
    resp = blob_client.get("/v1/table/clips/blobs", params={"column": "payload", "row": 2})
    assert resp.status_code == 200
    assert resp.content == b""
    assert resp.headers["content-length"] == "0"

    ranged = blob_client.get(
        "/v1/table/clips/blobs",
        params={"column": "payload", "row": 2},
        headers={"Range": "bytes=0-3"},
    )
    assert ranged.status_code == 416
    assert ranged.headers["content-range"] == "bytes */0"


def test_get_blob_version_param_over_http(blob_client: TestClient) -> None:
    # Overwrite bumps the head; ?version=1 pins the ORIGINAL bytes; a missing version is a 404
    # problem+json (not the raw lance ValueError as a 500); version=0 fails validation.
    from catalog.api.dependencies import get_namespace
    from catalog.services.dataplane import create_table as recreate

    ns = blob_client.app.dependency_overrides[get_namespace]()
    recreate(ns, {}, ["clips"], _blob_ipc([b"replacement"]), mode="overwrite")

    head = blob_client.get("/v1/table/clips/blobs", params={"column": "payload", "row": 0})
    assert head.status_code == 200 and head.content == b"replacement"

    pinned = blob_client.get("/v1/table/clips/blobs", params={"column": "payload", "row": 0, "version": 1})
    assert pinned.status_code == 200
    assert pinned.content == b"hello-world"
    assert pinned.headers["etag"] == '"1-payload-0"'

    missing = blob_client.get("/v1/table/clips/blobs", params={"column": "payload", "row": 0, "version": 999})
    assert missing.status_code == 404
    assert missing.json()["title"] == "TableVersionNotFoundError"

    zero = blob_client.get("/v1/table/clips/blobs", params={"column": "payload", "row": 0, "version": 0})
    assert zero.status_code == 422


def test_get_blob_if_range_downgrades_stale_resume_to_200(blob_client: TestClient) -> None:
    # RFC 9110 §13.1.5: a resuming client whose validator no longer matches must get the FULL
    # current payload (200), never a 206 spliced from a different incarnation; a matching
    # validator keeps the 206.
    stale = blob_client.get(
        "/v1/table/clips/blobs",
        params={"column": "payload", "row": 0},
        headers={"Range": "bytes=6-", "If-Range": '"999-payload-0"'},
    )
    assert stale.status_code == 200
    assert stale.content == b"hello-world"

    fresh = blob_client.get(
        "/v1/table/clips/blobs",
        params={"column": "payload", "row": 0},
        headers={"Range": "bytes=6-", "If-Range": '"1-payload-0"'},
    )
    assert fresh.status_code == 206
    assert fresh.content == b"world"
