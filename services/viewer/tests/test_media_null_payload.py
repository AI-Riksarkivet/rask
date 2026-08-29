"""A document with no media payload must 404, not 200-then-crash.

open_fastapi-audit — "`GET /api/media` answers 200 with Content-Length 0 and then crashes mid-stream
for a document whose media payload is NULL — two sibling helpers took opposite decisions on the same
value".

pylance 10.0.0 puts `None` in a null payload's slot where 9.0.0 omitted the row, and the two helpers
that meet it answered differently — each with a comment asserting ITS answer was the honest one:

  * `blob_size` returns 0 ("an absent payload is a zero-length body").
  * `stream_blob_range` raises `NotFoundError` ("a 404 is the honest answer and the one every other
    absence on this path already gives").

The non-range branch calls both, in that order. `total` came back 0, no Range header meant control
reached the `StreamingResponse`, and starlette's `stream_response` sends `http.response.start` FIRST
and only then iterates the body — so the 200 and `Content-Length: 0` were already on the wire when
the generator's first step raised. `register_handlers`' DomainError→404 mapping cannot see an
exception raised after the headers are sent. The caller got a 200 with an empty body where every
sibling absence on this path gives a 404 problem+json, and the server logged an unhandled-ASGI
traceback on every such request.

The rule is that a streaming generator must not raise once the headers are gone: by then the
handlers are out of reach and the status is already chosen. So the decision moves to the ROUTE,
before any response object exists — which is also the only place that can answer with a status.

The range branch was already fine and stays pinned: `parse_range(hdr, 0)` answers
`RangeVerdict.UNSATISFIABLE` because 0 > -1, so it answers a clean 416.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance import blob_array, blob_field
from viewer.api.v1.endpoints import media as media_ep

from service_kit.exceptions import NotFoundError


def _dataset(tmp_path: Path, payload: bytes | None) -> lance.LanceDataset:
    """A REAL one-row Lance dataset whose blob cell is null, through real pylance.

    Mocking `take_blobs` would prove nothing here: the whole defect is about what pylance 10.0.0
    actually puts in that slot, and a mock would simply restate the assumption under test.
    """
    schema = pa.schema([pa.field("doc_id", pa.string()), blob_field("media"), pa.field("mime", pa.string())])
    table = pa.table({"doc_id": ["doc-1"], "media": blob_array([payload]), "mime": ["audio/wav"]}, schema=schema)
    # `data_storage_version="2.2"` is mandatory, not tidiness: blob v2 refuses anything below it
    # ("Blob v2 requires file version >= 2.2"). `enable_stable_row_ids` matches how the medallion
    # writes, so the `_rowid` this test takes by is the one the route would resolve.
    return lance.write_dataset(
        table,
        str(tmp_path / "docs.lance"),
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )


def test_the_size_probe_reports_a_real_payloads_length(tmp_path: Path) -> None:
    """The probe still answers the size question it exists for."""
    ds = _dataset(tmp_path, b"RIFFsomewavbytes")
    assert media_ep.payload_size(ds, "media", 0) == len(b"RIFFsomewavbytes")


def test_a_null_payload_is_refused_BEFORE_a_response_exists(tmp_path: Path) -> None:
    """The route must decide, because it is the last place that can still choose a status."""
    ds = _dataset(tmp_path, None)
    with pytest.raises(NotFoundError):
        media_ep.payload_size(ds, "media", 0)


def test_a_present_payload_is_not_refused(tmp_path: Path) -> None:
    """The guard must not turn every document into a 404 — the failure mode that would hide the fix."""
    ds = _dataset(tmp_path, b"RIFFsomewavbytes")
    media_ep.payload_size(ds, "media", 0)


def test_the_range_branch_still_answers_416(tmp_path: Path) -> None:
    """The satisfiability answer for a zero-length body is 416, and it was already correct."""
    assert media_ep.parse_range("bytes=0-10", 0) is media_ep.RangeVerdict.UNSATISFIABLE
