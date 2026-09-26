"""XC-097: `CatalogTableReader` decodes the catalog's `/query` response through the validating decoder.

The response is bytes from another service. Its IPC framing can parse while its buffers lie, and a
table read from such a file without full validation carries values read from outside the response
(pyarrow 25.0.0) into whatever the caller does next.
"""

from __future__ import annotations

import struct
from typing import TYPE_CHECKING

import pyarrow as pa
import pytest

from service_kit.lancekit.arrow_ipc import ArrowBodyError
from service_kit.lancekit.reader import CatalogTableReader, CatalogVersion


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import QueryTableRequest


_OFFSETS = struct.pack("<iii", 0, 5, 10)


class _Answers:
    """A `CatalogTransport` whose `/query` answers the bytes it was given."""

    def __init__(self, response: bytes) -> None:
        self.response = response

    def query(self, request: QueryTableRequest) -> bytes:
        return self.response

    def count(self, table_id: list[str], filter: str | None, *, version: int | None = None) -> int:  # noqa: A002 — the transport protocol's own name
        raise AssertionError("not asked")

    def table_version(self, table_id: list[str]) -> int:
        raise AssertionError("not asked")

    def list_versions(self, table_id: list[str], *, limit: int | None = None) -> list[CatalogVersion]:
        raise AssertionError("not asked")


def _file(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


_TWO_BLOBS = pa.table({"v": pa.array([b"hello", b"world"], pa.binary())})


@pytest.mark.parametrize(
    "offsets",
    [pytest.param(struct.pack("<iii", 0, 5, 65536), id="past-the-values-buffer"), pytest.param(struct.pack("<iii", 0, 8, 5), id="that-decrease")],
)
def test_a_response_whose_buffers_lie_is_refused(offsets: bytes) -> None:
    body = _file(_TWO_BLOBS)
    assert body.count(_OFFSETS) == 1, "the offsets to tamper with are not unique in the response"
    reader = CatalogTableReader(_Answers(body.replace(_OFFSETS, offsets)), ["ns", "t"])

    with pytest.raises(ArrowBodyError):
        reader.to_table()


def test_a_valid_response_is_the_table_it_carries() -> None:
    reader = CatalogTableReader(_Answers(_file(_TWO_BLOBS)), ["ns", "t"])

    assert reader.to_table().equals(_TWO_BLOBS)
