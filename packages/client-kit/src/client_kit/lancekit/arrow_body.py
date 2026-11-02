"""A client door's Arrow body in either IPC framing, read from the body's own magic.

pyarrow writes a stream or a file depending on which writer a caller reached for, and a client door may take both.
The framing is read from the file magic, so a stream whose buffers fail validation is refused for its buffers, not
re-read as a file and refused for the wrong reason; the decoding itself is the one validator,
:func:`service_kit.lancekit.arrow_ipc.decode_arrow_body`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from service_kit.lancekit.arrow_ipc import decode_arrow_body


if TYPE_CHECKING:
    import pyarrow as pa


#: The IPC file format's leading magic (the Arrow columnar format's ``ARROW1``); a stream opens with a message length.
_FILE_MAGIC = b"ARROW1"


def decode_arrow_stream_or_file(data: bytes, *, max_bytes: int) -> pa.Table:
    """A caller's Arrow IPC stream or file, validated in full and held to ``max_bytes`` once inflated.

    Raises:
        TypeError: ``data`` is not bytes, or ``max_bytes`` is not an int.
        ValueError: ``max_bytes`` is below 1.
        ArrowBodyTooLargeError: the body is over ``max_bytes`` once inflated.
        ArrowBodyError: the body is not a valid Arrow IPC stream or file, or declares more than its bytes carry.
    """
    framing = "file" if isinstance(data, bytes) and data.startswith(_FILE_MAGIC) else "stream"
    return decode_arrow_body(data, max_bytes=max_bytes, framing=framing)
