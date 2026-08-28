"""A fanned-out transcription must come back attached to the line it came from.

Found by the Ray design-patterns audit (2026-08-28) against ray-project's own
`doc/source/ray-core/patterns/nested-tasks.rst`. The fan-out itself follows the
upstream guidance — every sub-request is submitted before the first `.result()` —
but the REASSEMBLY did not: the crops were round-robin sharded and the results
were flattened back in SHARD order, while `zip` paired them against `entries` in
SORTED order.

    entries      sorted by crop width:   e0 e1 e2 e3 e4 e5
    shards       round-robin:            [c0 c3] [c1 c4] [c2 c5]
    flat_results shard-concatenated:      r0 r3   r1 r4   r2 r5
    zip(entries, flat_results):          e0-r0  e1-r3  e2-r1  ...

So for any batch with four or more line crops — every real page batch — a line
carried someone else's transcription, across pages within the 64-row batch, and
nothing failed: `strict=True` guards LENGTH, not order. The sibling
`HTRFlowViaServe` rebuilds `flat_paths` FROM the shards before zipping, which is
the same fix and why the two now read alike.

The handle is injectable for the same reason `HTRFlowViaServeBytes`'s is: the
production path (`handle=None`) looks the app up through Serve, and a unit test
passes a fake.
"""

from __future__ import annotations

import numpy as np
import pytest


class _FakeRef:
    def __init__(self, value: list[tuple[str, float]]) -> None:
        self._value = value

    def result(self) -> list[tuple[str, float]]:
        return self._value


class _IdentityRemote:
    """Answers each crop with its OWN width, so a misattribution is visible."""

    def __init__(self) -> None:
        self.shards: list[list[int]] = []

    def remote(self, crops: list) -> _FakeRef:
        self.shards.append([c.width for c in crops])
        return _FakeRef([(f"w{c.width}", 1.0) for c in crops])


class _FakeHandle:
    def __init__(self) -> None:
        self.transcribe = _IdentityRemote()


def _batch(widths: list[int]) -> dict:
    """One page whose lines have the given (distinct) widths."""
    from io import BytesIO

    from PIL import Image

    from htr._columns import pack
    from htr.schemas import Line

    buf = BytesIO()
    Image.new("RGB", (400, 400), "white").save(buf, format="PNG")
    lines = [Line(x=0, y=0, w=w, h=20, confidence=1.0, abs_x=0, abs_y=i * 25) for i, w in enumerate(widths)]
    return {"image_bytes": np.array([buf.getvalue()], dtype=object), "lines": np.array([pack(lines)], dtype=object)}


@pytest.mark.parametrize("widths", [[30, 60, 90, 120, 150, 180], [40, 80, 120, 160]])
def test_each_line_gets_its_OWN_transcription(widths: list[int]) -> None:
    """Four or more crops is where the round-robin shards stop being the identity."""
    from htr._columns import unpack
    from runner.transcribe_service import TranscribeViaServe

    handle = _FakeHandle()
    out = TranscribeViaServe(handle=handle)(_batch(widths))

    assert len(handle.transcribe.shards) > 1, "the fan-out did not shard — the test would prove nothing"
    transcribed = unpack(out["transcribed"][0])
    assert [t.text for t in transcribed] == [f"w{w}" for w in widths], (
        "a line carried another line's transcription — the shard-order results were zipped against sorted entries"
    )


def test_a_single_crop_is_still_correct() -> None:
    """The degenerate case the old code did get right, kept so the fix cannot regress it."""
    from htr._columns import unpack
    from runner.transcribe_service import TranscribeViaServe

    out = TranscribeViaServe(handle=_FakeHandle())(_batch([55]))
    assert [t.text for t in unpack(out["transcribed"][0])] == ["w55"]
