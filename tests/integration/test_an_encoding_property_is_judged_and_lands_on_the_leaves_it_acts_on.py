"""A create's ``lance-encoding:*`` knobs are judged at the door and stamped on the fields each acts on ([[LH-246]]).

Lance reads each knob from a field's own metadata (`lance_docs/file_format.md` § Compression Configuration), and the
table keeps that metadata, so every later write encodes under it. Measured on pylance 13.0.0: an empty create
holding `compression=bogus` commits and every later append fails `OSError: Unknown compression scheme`, and
`dict-size-ratio=7` raises pyo3's `PanicException` on the first low-cardinality page. A value Lance cannot honour,
in the properties or on the payload's own fields, is therefore refused 400 at the create, before anything is written.

Nothing is set by default (ADR 0066, [[LH-034]]): a plain create stamps no knob.

Driven over HTTP against a real `dir` backend; the stamped metadata is read off the dataset the create wrote.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from urllib.parse import quote

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    import httpx
    from fastapi.testclient import TestClient

ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}
INVALID_INPUT = 13
TABLE = "/v1/table/db$t"
PREFIX = b"lance-encoding:"

#: One field of each shape the knobs tell apart: 64-, 32- and 16-bit fixed width, boolean, variable width, a list's
#: item, a struct's children, and a fixed-size list (whose item Lance encodes under no knob, measured).
SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("score", pa.float64()),
        pa.field("half", pa.float16()),
        pa.field("flag", pa.bool_()),
        pa.field("name", pa.string()),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("point", pa.struct([pa.field("x", pa.float32()), pa.field("label", pa.string())])),
        pa.field("vec", pa.list_(pa.float32(), 2)),
    ]
)
LEAVES = {"id", "score", "half", "flag", "name", "tags.item", "point.x", "point.label"}


def _create(client: TestClient, properties: dict[str, str], schema: pa.Schema = SCHEMA) -> httpx.Response:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, schema) as writer:
        writer.write_table(schema.empty_table())
    query = f"?properties={quote(json.dumps(properties))}" if properties else ""
    return client.post(f"{TABLE}/create{query}", content=sink.getvalue().to_pybytes(), headers=ARROW_STREAM)


def _fields(fields: Iterable[pa.Field], prefix: str = "") -> Iterator[tuple[str, dict[bytes, bytes]]]:
    for field in fields:
        path = f"{prefix}{field.name}"
        yield path, dict(field.metadata or {})
        yield from _fields((field.type.field(i) for i in range(field.type.num_fields)), f"{path}.")


def _encoding_on_each_field(client: TestClient) -> dict[str, dict[str, str]]:
    described = client.post(f"{TABLE}/describe", json={})
    assert described.status_code == 200, described.text
    stamped = {
        path: {k.decode(): v.decode() for k, v in meta.items() if k.startswith(PREFIX)}
        for path, meta in _fields(lance.dataset(described.json()["table_uri"]).schema)
    }
    return {path: knobs for path, knobs in stamped.items() if knobs}


def _with_item_metadata(metadata: dict[str, str]) -> pa.Schema:
    """SCHEMA with ``metadata`` on the list's item, so a judge reading only top-level fields misses it."""
    tags = pa.field("tags", pa.list_(pa.field("item", pa.string(), metadata=metadata)))
    return SCHEMA.set(SCHEMA.get_field_index("tags"), tags)


@pytest.fixture
def client(real_ns_client: TestClient) -> TestClient:
    assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
    return real_ns_client


@pytest.mark.parametrize(
    ("properties", "schema", "detail"),
    [
        pytest.param(
            {"lance-encoding:compression": "bogus"},
            SCHEMA,
            "lance-encoding:compression='bogus' is not accepted: Lance takes one of ['fsst', 'lz4', 'none', 'zstd']",
            id="a-scheme-lance-does-not-know",
        ),
        pytest.param(
            {"lance-encoding:rle-threshold": "1.5"},
            SCHEMA,
            "lance-encoding:rle-threshold='1.5' is not accepted: Lance takes a decimal from 0 to 1",
            id="a-ratio-past-one",
        ),
        pytest.param(
            {"lance-encoding:dict-size-ratio": "0"},
            SCHEMA,
            "lance-encoding:dict-size-ratio='0' is not accepted: Lance takes a decimal above 0 and at most 1",
            id="a-ratio-of-zero-where-lance-panics",
        ),
        pytest.param(
            {"lance-encoding:dict-divisor": "1"},
            SCHEMA,
            "lance-encoding:dict-divisor='1' is not accepted: Lance takes an integer greater than 1",
            id="a-divisor-of-one",
        ),
        pytest.param(
            {"lance-encoding:compression": "lz4", "lance-encoding:compression-level": "9"},
            SCHEMA,
            "lance-encoding:compression-level is accepted only beside lance-encoding:compression=zstd: no other scheme takes a level",
            id="a-level-beside-lz4",
        ),
        pytest.param(
            {"lance-encoding:compression": "zstd", "lance-encoding:compression-level": "23"},
            SCHEMA,
            "lance-encoding:compression-level='23' is not accepted: zstd takes a level from 0 to 22",
            id="a-zstd-level-past-22",
        ),
        pytest.param(
            {"lance-encoding:structural-encoding": "fullzip"},
            SCHEMA,
            "lance-encoding:structural-encoding is not accepted: a forced layout overrides Lance's own choice on every later write to the table",
            id="a-forced-structural-encoding",
        ),
        pytest.param(
            {"lance-encoding:compresion": "zstd"},
            SCHEMA,
            "lance-encoding:compresion is not an encoding knob Lance defines; the knobs are ['lance-encoding:bss', 'lance-encoding:compression', "
            "'lance-encoding:compression-level', 'lance-encoding:dict-divisor', 'lance-encoding:dict-size-ratio', "
            "'lance-encoding:dict-values-compression', 'lance-encoding:dict-values-compression-level', 'lance-encoding:general', "
            "'lance-encoding:packed', 'lance-encoding:rle-threshold']",
            id="a-knob-lance-does-not-define",
        ),
        pytest.param(
            {},
            _with_item_metadata({"lance-encoding:dict-size-ratio": "7"}),
            "lance-encoding:dict-size-ratio='7' is not accepted: Lance takes a decimal above 0 and at most 1",
            id="a-bad-value-on-a-payload-fields-own-metadata",
        ),
    ],
)
def test_an_encoding_value_lance_cannot_honour_is_refused_at_create(client: TestClient, properties: dict[str, str], schema: pa.Schema, detail: str) -> None:
    refused = _create(client, properties, schema)

    assert (refused.status_code, refused.headers["content-type"]) == (400, "application/problem+json"), refused.text
    assert (refused.json()["code"], refused.json()["detail"]) == (INVALID_INPUT, detail)
    assert client.post(f"{TABLE}/describe", json={}).status_code == 404, "a refused create left a table behind"


@pytest.mark.parametrize(
    ("properties", "lands_on"),
    [
        pytest.param({}, set(), id="a-plain-create-stamps-nothing"),
        pytest.param({"lance-encoding:compression": "zstd"}, LEAVES, id="general-compression-on-every-leaf"),
        pytest.param({"lance-encoding:rle-threshold": "0.2"}, LEAVES - {"flag"}, id="rle-on-every-leaf-but-boolean"),
        pytest.param({"lance-encoding:bss": "on"}, {"id", "score", "point.x"}, id="bss-on-the-32-and-64-bit-numeric-leaves"),
        pytest.param({"lance-encoding:packed": "true"}, {"point"}, id="packing-on-the-struct"),
    ],
)
def test_each_encoding_knob_lands_on_the_fields_it_acts_on(client: TestClient, properties: dict[str, str], lands_on: set[str]) -> None:
    created = _create(client, properties)
    assert created.status_code == 200, created.text

    assert _encoding_on_each_field(client) == dict.fromkeys(lands_on, properties)
