"""The table client vectors of ``gen_port_vectors.py``: what the Python catalog client sends and what it makes of an answer.

Four groups, each from the real Python code:

- ``sql_literal`` and ``in_lists``: ``lakehouse.lancekit.filters`` over a corpus of values.
- ``errors``: what ``translate_catalog_errors`` raises for a catalog's answer of a given status and body.
- ``requests``: each call lineage's provenance store makes on the reader and writer transports (and the two it makes on
  the generated client itself, ``create_table`` and ``describe_table``), sent through the real generated client. Only the
  socket is replaced: the client's REST object hands each request to a recorder, which answers it with a recorded
  response, so what is recorded is the method, URL, headers and body the client would put on the wire, and the result
  the Python code made of the answer.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch


_TOKEN = "recorded-bearer"  # noqa: S105 - a fixture's, protecting nothing
_ID = ["lance_catalog", "provenance", "events"]
_BASE = "http://catalog.test"
_UUID = "0190a1b2-0000-7000-8000-0000000000aa"
#: The headers a request's meaning rides in; the client's user agent is its own.
_HEADERS = ("Accept", "Content-Type", "Authorization")


def _literals() -> list[dict[str, Any]]:
    from lakehouse.lancekit.filters import sql_literal

    values = ["plain", "it's", "''", "", "a'b'c", "å ö 中 \U0001f4c4", "back\\slash", "nl\nnul\x00", "' OR 1=1 --"]
    return [{"value": value, "literal": sql_literal(value)} for value in values]


def _in_lists() -> list[dict[str, Any]]:
    from lakehouse.lancekit.filters import IN_LIST_SIZE, in_lists

    def keys(n: int) -> list[str]:
        # Written out of order, so the sort is what puts them in order.
        return [f"k{(n - i) * 7919 % 100003:06d}" for i in range(n)]

    corpus = {
        "none": [],
        "one": ["only"],
        "quoted": ["it's", "a", "b'"],
        "duplicates": ["b", "a", "b"],
        "outside ascii, in code point order": ["ö", "z", "å", "\U0001f4c4", "中", "Z"],
        "a full list": keys(IN_LIST_SIZE),
        "one over a full list": keys(IN_LIST_SIZE + 1),
        "two lists and one more": keys(2 * IN_LIST_SIZE + 1),
    }
    return [{"name": name, "column": "event_key", "values": values, "filters": list(in_lists("event_key", values))} for name, values in corpus.items()]


def _errors() -> list[dict[str, Any]]:
    from lance_namespace_urllib3_client import exceptions as api

    from lakehouse.exceptions import DomainError
    from lakehouse.lancekit.reader import translate_catalog_errors

    by_status: dict[int, type[api.ApiException]] = {
        400: api.BadRequestException,
        401: api.UnauthorizedException,
        403: api.ForbiddenException,
        404: api.NotFoundException,
        409: api.ConflictException,
        422: api.UnprocessableEntityException,
    }

    def problem(code: int, status: int, detail: str) -> str:
        return json.dumps({"type": "about:blank", "title": "t", "status": status, "detail": detail, "code": code, "error": detail})

    answers = [
        (400, problem(13, 400, "bad filter")),
        (400, "not json"),
        (401, problem(16, 401, "no bearer")),
        (403, problem(15, 403, "no grant")),
        (404, problem(4, 404, "no such table")),
        (404, problem(11, 404, "no such version")),
        (404, problem(13, 400, "a code that disagrees with its status")),
        (409, problem(14, 409, "commit conflict")),
        (409, problem(5, 409, "exists")),
        (413, problem(13, 413, "too large")),
        (422, problem(13, 400, "unprocessable")),
        (429, problem(21, 429, "slow down")),
        (500, problem(18, 500, "Internal Server Error")),
        (502, "<html>bad gateway</html>"),
        (503, problem(17, 503, "down")),
        (504, ""),
    ]
    vectors: list[dict[str, Any]] = []
    for status, body in answers:
        raised: DomainError | None = None
        with patch("lakehouse.lancekit.reader.log"):
            try:
                with translate_catalog_errors():
                    raise by_status.get(status, api.ServiceException if status >= 500 else api.ApiException)(status=status, reason="r", body=body)
            except DomainError as exc:
                raised = exc
        assert raised is not None
        vectors.append({"status": status, "body": body, "raised_status": int(raised.status_code), "message": str(raised)})
    return vectors


class _Answer:
    """What the generated client reads a urllib3 response through: a status, a reason, the body and the headers."""

    def __init__(self, status: int, data: bytes, content_type: str) -> None:
        self.status, self.reason, self.data = status, "OK", data
        self.headers = {"content-type": content_type}


def _rows() -> Any:  # noqa: ANN401 - a pyarrow table, imported here
    import pyarrow as pa

    return pa.table({"event_key": pa.array(["a", "b'", "å"], pa.string()), "seq": pa.array([1, 2, None], pa.int64())})


def _arrow_file(table: Any) -> bytes:  # noqa: ANN401 - a pyarrow table
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _answer_for(url: str) -> _Answer:
    """The recorded catalog's answer to a request, by the door it reached."""
    door = url.split("?")[0].rsplit("/", 1)[-1]
    if door == "query":
        return _Answer(200, _arrow_file(_rows()), "application/vnd.apache.arrow.file")
    if door == "count_rows":
        return _Answer(200, b"7", "application/json")
    if door == "describe":
        return _Answer(200, json.dumps({"version": 9, "location": "s3://b/t", "properties": {"rask.uuid": _UUID, "other": "x"}}).encode(), "application/json")
    return _Answer(200, json.dumps({"version": 10}).encode(), "application/json")


def _calls(token: str | None) -> list[tuple[str, dict[str, Any], Callable[[], Any]]]:
    """Each call the provenance store makes: a name, the call as data for the port to make, and the Python making it."""
    from lance_namespace_urllib3_client.api.data_api import DataApi
    from lance_namespace_urllib3_client.api.table_api import TableApi
    from lance_namespace_urllib3_client.models import DescribeTableRequest

    from lakehouse.arrow_ipc import encode_arrow_stream
    from lakehouse.lancekit.catalog_client import catalog_api_client, catalog_read_client, request_headers
    from lakehouse.lancekit.reader import CatalogTableReader, RestCatalogTransport
    from lakehouse.lancekit.writer import RestCatalogWriteTransport

    reader = CatalogTableReader(RestCatalogTransport(_BASE, token=token), _ID)
    writer = RestCatalogWriteTransport(_BASE, _ID, token=token)

    def create() -> None:
        api = DataApi(catalog_api_client(_BASE))
        api.create_table("$".join(_ID), encode_arrow_stream(_rows().schema.empty_table()), delimiter="$", mode="exist_ok", _headers=request_headers(token))

    def properties() -> dict[str, str] | None:
        request = DescribeTableRequest(vend_credentials=False)
        return TableApi(catalog_read_client(_BASE)).describe_table("$".join(_ID), request, delimiter="$", _headers=request_headers(token)).properties

    def read(**scan: Any) -> list[dict[str, Any]]:  # noqa: ANN401 - a scan's arguments
        return reader.to_table(**scan).to_pylist()

    at_version = {"columns": ["event_key", "seq"], "filter": "seq >= 1", "limit": 3, "offset": 2, "with_row_id": True, "version": 4}
    upsert = {"on": "event_key", "matched_when": "source.seq > target.seq"}
    return [
        ("create, keeping a table that exists", {"op": "create"}, create),
        ("describe, for the uuid", {"op": "describe"}, properties),
        ("the current version", {"op": "version"}, reader.table_version),
        ("count every row", {"op": "count", "filter": None, "version": None}, lambda: reader.count_rows()),
        ("count under a filter at a version", {"op": "count", "filter": "seq > 1", "version": 3}, lambda: reader.count_rows("seq > 1", version=3)),
        ("read at a version", {"op": "read"} | at_version, lambda: read(**at_version)),
        ("read the current version", {"op": "read", "limit": 3}, lambda: read(limit=3)),
        ("insert only on a key", {"op": "merge_insert_only", "on": "event_key"}, lambda: writer.merge_insert_only(_rows(), on="event_key")),
        ("upsert on a key", {"op": "merge_upsert", "on": "event_key", "matched_when": None}, lambda: writer.merge_upsert(_rows(), on="event_key")),
        ("upsert where a condition holds", {"op": "merge_upsert"} | upsert, lambda: writer.merge_upsert(_rows(), **upsert)),
        ("delete by a predicate", {"op": "delete", "predicate": "event_key = 'b'''"}, lambda: writer.delete("event_key = 'b'''")),
    ]


def _requests() -> list[dict[str, Any]]:
    from lance_namespace_urllib3_client import rest

    from lakehouse.lancekit.catalog_client import reset_catalog_clients

    sent: list[dict[str, Any]] = []

    def record(_: object, method: str, url: str, headers: dict[str, str] | None = None, body: Any = None, **__: Any) -> rest.RESTResponse:  # noqa: ANN401
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
        kept = {name.lower(): value for name, value in (headers or {}).items() if name in _HEADERS}
        sent.append({"method": method, "url": url.removeprefix(_BASE), "headers": kept, "body": base64.b64encode(data).decode()})
        return rest.RESTResponse(_answer_for(url))

    vectors: list[dict[str, Any]] = []
    with patch.object(rest.RESTClientObject, "request", record):
        for token in (_TOKEN, None):
            reset_catalog_clients()
            for name, spec, call in _calls(token):
                sent.clear()
                result = call()
                answers = [_answer_for(request["url"]) for request in sent]
                exchanges = [
                    request | {"answer": {"content_type": answer.headers["content-type"], "body": base64.b64encode(answer.data).decode()}}
                    for request, answer in zip(sent, answers, strict=True)
                ]
                vectors.append({"name": name, "token": token, "table_id": _ID, "call": spec, "exchanges": exchanges, "result": result})
    reset_catalog_clients()
    return vectors


def tables_vectors() -> dict[str, list[Any]]:
    return {"sql_literal": _literals(), "in_lists": _in_lists(), "errors": _errors(), "requests": _requests()}
