"""A JSON value that does not parse is the caller's input, refused 400 code 13 at every door that writes rows.

Lance parses a JSON value only when it encodes it, deep in the write, and reports one that does not parse as an untyped
``OSError`` ("Failed to encode JSON", measured on pylance 13.0.0), which the create and insert doors answered 500. It
encodes a JSON value nested in a list or a map, and one beneath a null list, so the refusal reads those too. Driven over
HTTP against a real ``dir`` backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import lance
import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


if TYPE_CHECKING:
    import httpx
    from fastapi.testclient import TestClient


def _json(*docs: str) -> pa.Array:
    return pa.array(docs, type=pa.json_(pa.utf8()))


def _rows(row: int, doc: pa.Array) -> bytes:
    return encode_arrow_stream(pa.table({"id": pa.array([row], pa.int64()), "doc": doc}))


def _write(client: TestClient, door: str, body: bytes) -> httpx.Response:
    return client.post(f"/v1/table/{door}", content=body, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})


def _version(client: TestClient) -> int:
    return lance.dataset(client.post("/v1/table/lance_catalog$db$t/describe", json={}).json()["table_uri"]).version


@pytest.mark.parametrize(
    ("door", "body", "refusal"),
    [
        pytest.param("lance_catalog$db$t/insert", _rows(2, _json("{not json")), "column 'doc' holds a value that is not JSON at position 0", id="insert"),
        pytest.param(
            "lance_catalog$db$t/merge_insert?on=id&when_not_matched_insert_all=true",
            _rows(2, _json("{not json")),
            "column 'doc' holds a value that is not JSON at position 0",
            id="merge_insert",
        ),
        pytest.param(
            "lance_catalog$db$fresh/create",
            _rows(2, pa.ListArray.from_arrays(pa.array([0, 1], pa.int32()), _json("{not json"), mask=pa.array([True]))),
            "column 'doc[]' holds a value that is not JSON at position 0",
            id="beneath-a-null-list",
        ),
        pytest.param(
            "lance_catalog$db$fresh/create",
            _rows(2, pa.MapArray.from_arrays(pa.array([0, 1], pa.int32()), pa.array(["k"]), _json("{not json"))),
            "column 'doc[].value' holds a value that is not JSON at position 0",
            id="a-maps-value",
        ),
    ],
)
def test_a_value_that_is_not_json_is_refused_and_nothing_is_written(real_ns_client: TestClient, door: str, body: bytes, refusal: str) -> None:
    assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
    assert _write(real_ns_client, "lance_catalog$db$t/create", _rows(1, _json('{"a": 1}'))).status_code == 200
    before = _version(real_ns_client)

    refused = _write(real_ns_client, door, body)

    assert (refused.status_code, refused.headers["content-type"], refused.json()["code"]) == (400, "application/problem+json", 13), refused.text
    assert refused.json()["detail"].startswith(refusal), refused.text
    assert _version(real_ns_client) == before
    assert real_ns_client.post("/v1/table/lance_catalog$db$fresh/exists", json={}).status_code == 404
