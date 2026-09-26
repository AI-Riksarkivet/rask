"""A create refused for its SHAPE holds no idempotency key, so the corrected retry under that key runs.

The create door's claim is an object-store record with a 300 s lease. A refusal raised after the claim
leaves it in flight, and the caller who fixes the request and retries under the same key is answered
409 `ConcurrentModification` for an attempt that never ran. So every refusal that needs nothing but the
request runs ahead of the claim: the mode, a wildcard segment, an off-allowlist `data_base`, `properties`
that are not a JSON map of strings (spec.yaml:3761-3767), a non-Lance format, the derived-write pin
(`source` / `source_version`), the `X-Lance-Run-Facets` header, and a body that is not a valid Arrow
IPC stream, in its framing or in its buffers.

Driven over HTTP against a real `dir` backend, each as a keyed typo followed by the fixed request under
the same key, so the second status is read where a retrying client reads it.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}
INVALID_INPUT = 13
CREATE = "/v1/table/db$t/create"
#: Past CPython's int-string conversion limit, so `json.loads` raises a bare `ValueError` rather than
#: `JSONDecodeError` — valid JSON Python will not parse.
_UNPARSEABLE_INT = "1" * 5000
#: `json.loads` raises `RecursionError` from a nesting depth of 10000 (CPython 3.13.12); the encoded URL
#: stays under httpx's 65536-byte query limit.
_NESTED_PAST_THE_RECURSION_LIMIT = "[" * 10500 + "]" * 10500


def _stream(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def _rows(n: int = 3) -> bytes:
    return _stream(pa.table({"id": pa.array(range(n), pa.int64())}))


def _tampered(kind: pa.DataType, good: bytes, bad: bytes) -> bytes:
    """A two-row stream whose framing still parses after `good` is overwritten with `bad` in its buffers."""
    values = [b"hello", b"world"] if kind == pa.binary() else ["hello", "world"]
    body = _stream(pa.table({"v": pa.array(values, kind)}))
    assert body.count(good) == 1, "the bytes to tamper with are not unique in the stream"
    return body.replace(good, bad)


_OFFSETS = struct.pack("<iii", 0, 5, 10)


@pytest.mark.parametrize(
    ("malformed", "headers", "refusal"),
    [
        pytest.param("/v1/table/db$t*/create", {}, "reserved", id="a-wildcard-segment"),
        pytest.param(f"{CREATE}?data_base=s3://rogue", {}, "allowlist", id="an-off-allowlist-data-base"),
        pytest.param(f"{CREATE}?properties={{not-json", {}, "not valid JSON", id="properties-that-are-not-json"),
        pytest.param(f'{CREATE}?properties={{"a":{_UNPARSEABLE_INT}}}', {}, "not valid JSON", id="properties-python-cannot-parse"),
        pytest.param(f"{CREATE}?properties={_NESTED_PAST_THE_RECURSION_LIMIT}", {}, "not valid JSON", id="properties-nested-past-the-recursion-limit"),
        pytest.param(f"{CREATE}?properties=[1]", {}, "string values", id="properties-that-are-an-array"),
        pytest.param(f'{CREATE}?properties="x"', {}, "string values", id="properties-that-are-a-string"),
        pytest.param(f'{CREATE}?properties={{"a":1}}', {}, "string values", id="a-property-that-is-a-number"),
        pytest.param(f'{CREATE}?properties={{"a":null}}', {}, "string values", id="a-property-that-is-null"),
        pytest.param(f'{CREATE}?properties={{"write.format.default":"parquet"}}', {}, "Lance only", id="a-non-lance-format"),
        pytest.param(f"{CREATE}?source_version=3", {}, "source_version requires source", id="a-source-version-with-no-source"),
        pytest.param(f"{CREATE}?source=%24&source_version=1", {}, "not a valid dataset id", id="a-delimiter-only-source"),
        pytest.param(CREATE, {"X-Lance-Run-Facets": "{not-json"}, "must be valid JSON", id="run-facets-that-are-not-json"),
        pytest.param(CREATE, {"X-Lance-Run-Facets": "[1]"}, "must be a JSON object", id="run-facets-that-are-not-an-object"),
    ],
)
def test_a_keyed_shape_refusal_lets_the_corrected_retry_run(real_ns_client: TestClient, malformed: str, headers: dict[str, str], refusal: str) -> None:
    assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
    keyed = {**ARROW_STREAM, "Idempotency-Key": "typo-then-fix"}

    refused = real_ns_client.post(malformed, content=_rows(), headers={**keyed, **headers})
    assert refused.status_code == 400, f"the malformed create was not refused 400: {refused.status_code} {refused.text[:300]}"
    assert refused.json().get("code") == INVALID_INPUT, refused.json()
    assert refusal in str(refused.json().get("detail")), f"refused for another reason than the one under test: {refused.json()}"

    retried = real_ns_client.post(CREATE, content=_rows(), headers=keyed)

    assert retried.status_code == 200, f"the corrected retry under the same key was refused: {retried.status_code} {retried.text[:300]}"
    counted = real_ns_client.post("/v1/table/db$t/count_rows", json={})
    assert (counted.status_code, counted.text) == (200, "3"), counted.text


@pytest.mark.parametrize(
    "body",
    [
        # pyarrow raises `ArrowInvalid` for the first and a bare `OSError` for the second (pyarrow 25.0.0).
        pytest.param(b"this is not an arrow ipc stream", id="a-body-that-is-not-arrow"),
        pytest.param(_rows()[:-20], id="a-stream-cut-inside-its-batch"),
        # The rest frame cleanly and read without error; only `Table.validate(full=True)` refuses the
        # last two (pyarrow 25.0.0). Unvalidated, Lance persists 65,531 bytes of process heap for the
        # first, and refuses the others only after the claim.
        pytest.param(_tampered(pa.binary(), _OFFSETS, struct.pack("<iii", 0, 5, 65536)), id="binary-offsets-past-the-values-buffer"),
        pytest.param(_tampered(pa.string(), _OFFSETS, struct.pack("<iii", 0, 5, 65536)), id="utf8-offsets-past-the-values-buffer"),
        pytest.param(_tampered(pa.binary(), _OFFSETS, struct.pack("<iii", 0, 8, 5)), id="binary-offsets-that-decrease"),
        pytest.param(_tampered(pa.string(), b"hello", b"\xff\xfe\xfdlo"), id="utf8-values-that-are-not-utf8"),
    ],
)
def test_a_keyed_body_that_is_not_an_arrow_stream_lets_the_corrected_retry_run(real_ns_client: TestClient, body: bytes) -> None:
    assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
    keyed = {**ARROW_STREAM, "Idempotency-Key": "typo-then-fix"}

    refused = real_ns_client.post(CREATE, content=body, headers=keyed)
    assert refused.status_code == 400, f"the malformed body was not refused 400: {refused.status_code} {refused.text[:300]}"
    assert refused.json().get("code") == INVALID_INPUT, refused.json()
    assert "not an Arrow IPC stream" in str(refused.json().get("detail")), refused.json()

    retried = real_ns_client.post(CREATE, content=_rows(), headers=keyed)

    assert retried.status_code == 200, f"the corrected retry under the same key was refused: {retried.status_code} {retried.text[:300]}"
    counted = real_ns_client.post("/v1/table/db$t/count_rows", json={})
    assert (counted.status_code, counted.text) == (200, "3"), counted.text


def test_string_properties_still_create(real_ns_client: TestClient) -> None:
    """The positive half: a spec-shaped map passes the shape check and lands on the table."""
    assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200

    created = real_ns_client.post(f'{CREATE}?properties={{"team":"eng"}}', content=_rows(), headers=ARROW_STREAM)

    assert created.status_code == 200, created.text
    described = real_ns_client.post("/v1/table/db$t/describe", json={}).json()
    assert (described.get("properties") or {}).get("team") == "eng", described
