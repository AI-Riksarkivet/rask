"""The ONE Arrow-IPC **stream** encoder and its media type.

This is the wire the Lance catalog's write doors (``/v1/table/{id}/create``, ``/insert``,
``/merge_insert``) parse and the annotation engine consumes zero-copy. It is a **stream**, never an
IPC *file*: a file body fails the catalog's Rust reader with "failed to fill whole buffer". The read
side returns IPC files — that asymmetry is the catalog's contract, not this encoder's choice.

Ten modules across the fleet hand-rolled the same three lines and seven more hand-typed the media
type; both now live here so a change to the wire is a one-line change, not a fleet-wide grep.

``pyarrow`` is imported inside :func:`encode_arrow_stream`, not at module scope, so a module that only
needs :data:`ARROW_STREAM_MEDIA_TYPE` (a header value on a publish path that keeps the heavy import
off its own import time) can take the constant without pulling pyarrow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import pyarrow as pa


#: The Arrow-IPC **stream** media type — the ``Content-Type`` every catalog write body carries and the
#: type the ``/points`` / annotations read responses are served as. One spelling, one import.
ARROW_STREAM_MEDIA_TYPE = "application/vnd.apache.arrow.stream"


def encode_arrow_stream(table: pa.Table) -> bytes:
    """Serialize ``table`` as an Arrow-IPC stream. Pass ``schema.empty_table()`` to encode an
    empty, correctly-typed body (the shape a ``create`` sends)."""
    import pyarrow as pa

    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()
