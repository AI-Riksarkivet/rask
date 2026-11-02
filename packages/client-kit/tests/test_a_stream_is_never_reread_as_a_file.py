"""A client door's Arrow body is read in the framing its own magic names, by the one validator.

That a file-framed body decodes is pinned through the annotator's import (`test_the_ARROW_FILE_framing_is_accepted_too`).
"""

from __future__ import annotations

import struct

import pyarrow as pa
import pytest

from client_kit.lancekit.arrow_body import decode_arrow_stream_or_file
from service_kit.arrow_ipc import ArrowBodyError, encode_arrow_stream


_LIMIT = 1024 * 1024
_TABLE = pa.table({"v": pa.array([b"hello", b"world"], pa.binary())})


def test_a_stream_refused_for_its_buffers_is_refused_for_its_buffers() -> None:
    """A stream is never re-read as a file and refused for a reason that has nothing to do with what is wrong with it."""
    body = encode_arrow_stream(_TABLE)
    good = struct.pack("<iii", 0, 5, 10)
    assert body.count(good) == 1
    tampered = body.replace(good, struct.pack("<iii", 0, 8, 5))

    with pytest.raises(ArrowBodyError, match="non-monotonic offset"):
        decode_arrow_stream_or_file(tampered, max_bytes=_LIMIT)
