"""A byte response must declare what it actually is, and the seam must not name a modality.

open_fastapi-audit — "`GET /api/page` stamps `Content-Type: image/jpeg` on an opaque governed payload
— the tier schema carries no MIME and the table is a free query parameter".

`table` is a caller-supplied catalog table id and the governed tier schema is
`{id, payload, stage, lineage, source_rowid}` with `payload` OPAQUE — there is no MIME to read. The
route asserted `image/jpeg` anyway.

TWO DEFECTS, and the audit is careful that the second is the heavier one.

**The header.** A guess is worse than an honest default: `application/octet-stream` says "bytes I
cannot describe", which is true, while `image/jpeg` says something false about a PNG, a WAV or a PDF.
Graded low because the bytes are correct, the route is FGA-gated, and no known client dispatches on
it — a mis-rendered preview, not a data defect.

**The vocabulary, which is the load-bearing half.** `has_image`, "image bytes", "harvest produced
none" assert a MODALITY in a service seam over an arbitrary table. That is the
platform-knows-no-workload ruling, not file-handling: the same route serves audio, video and PDF
corpora, and CLAUDE.md's test for every shared seam is "would this be right for audio?"

SNIFFED, then defaulted. Magic bytes are the only evidence available when the schema carries none,
and they are evidence rather than assumption — a JPEG really does start `FF D8 FF`.
"""

from __future__ import annotations

import pytest

from viewer.api.v1.endpoints import pages as pages_ep


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 8
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
PDF = b"%PDF-1.7\n" + b"\x00" * 8
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 8


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (JPEG, "image/jpeg"),
        (WAV, "audio/wav"),
        (b"\x00\x01\x02\x03nothing recognisable", "application/octet-stream"),
    ],
    ids=["jpeg", "wav", "unknown"],
)
def test_the_media_type_is_sniffed_not_assumed(payload: bytes, expected: str) -> None:
    """The same route serves every modality; only the bytes can say which one this is."""
    assert pages_ep.sniff_media_type(payload) == expected
