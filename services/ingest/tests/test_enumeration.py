"""Enumeration must not FETCH — and the fetcher must resolve by scheme.

Two halves of the same seam. Enumeration runs once, in an activity, to slice a run into chunks;
fetching runs later, in workers, under the queue's backpressure. The first version enumerated through
`iter_objects()`, which reads every object's bytes to hand back its `uri` — so an n-object source was
transferred twice, the first time inside one activity with no backpressure at all. Against a
rate-limited IIIF volume that is a full download of the volume before a single unit is queued.

The counting double below is the only honest way to assert it: "did not fetch" is not observable
from the return value, which is identical either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ingest.fetch import UriFetcher
from ingest.sources import iter_unit_keys
from ingest.validation import PayloadValidator


class _CountingSource:
    """A source that records how many object bodies were read."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = keys
        self.bodies_read = 0

    def iter_keys(self):
        return iter(self._keys)

    def iter_objects(self):
        from service_kit.lakehouse.sources import SourceObject

        for key in self._keys:
            self.bodies_read += 1
            yield SourceObject(uri=key, data=b"x")


class _UnkeyedSource:
    """A source that genuinely cannot list without reading — the fallback path."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def iter_objects(self):
        from service_kit.lakehouse.sources import SourceObject

        for key in self._keys:
            yield SourceObject(uri=key, data=b"x")


def test_enumeration_reads_NO_object_bodies_when_the_source_can_list() -> None:
    """The measured defect, pinned. Same keys, zero transfers."""
    source = _CountingSource(["s3://b/1.tif", "s3://b/2.tif"])

    assert list(iter_unit_keys(source)) == ["s3://b/1.tif", "s3://b/2.tif"]
    assert source.bodies_read == 0


def test_a_source_that_cannot_list_still_enumerates() -> None:
    """The capability is optional — a source with no cheap listing must not become unusable."""
    assert list(iter_unit_keys(_UnkeyedSource(["iiif://a", "iiif://b"]))) == ["iiif://a", "iiif://b"]


def test_LocalDirSource_lists_the_same_keys_it_would_yield_objects_for(tmp_path: Path) -> None:
    """The two paths must not disagree, or a chunk's keys would name objects the worker cannot find.

    Order matters as much as membership: the bronze `id` is derived from the key, and a differing
    order between enumeration and fetch would be invisible until someone compared two runs.
    """
    from service_kit.lakehouse.sources import LocalDirSource

    for name in ("b.tif", "a.tif", "c.tif"):
        (tmp_path / name).write_bytes(b"data")
    source = LocalDirSource(tmp_path)

    assert list(source.iter_keys()) == [obj.uri for obj in source.iter_objects()]


def test_the_fetcher_resolves_a_file_uri(tmp_path: Path) -> None:
    """`file://` is the A11 fixture lane — no network, runnable on a laptop and in CI."""
    import asyncio

    target = tmp_path / "page.tif"
    target.write_bytes(b"TIFF-BYTES")

    assert asyncio.run(UriFetcher().fetch(target.resolve().as_uri())) == b"TIFF-BYTES"


def test_the_fetcher_decodes_a_percent_encoded_path(tmp_path: Path) -> None:
    """`Path.as_uri()` percent-encodes, so a fixture named `sida 1.tif` round-trips as `sida%201.tif`.

    Without `unquote` the read fails with a FileNotFoundError naming a path that visibly exists on
    disk — the kind of error that sends someone looking at permissions and mounts.
    """
    import asyncio

    target = tmp_path / "sida 1.tif"
    target.write_bytes(b"SPACED")

    assert asyncio.run(UriFetcher().fetch(target.resolve().as_uri())) == b"SPACED"


def test_the_fetcher_refuses_an_unknown_scheme_by_NAME() -> None:
    """A worker that cannot fetch must say which scheme and which key.

    'no fetcher' alone would leave an operator grepping adapters; the message is the diagnosis.
    """
    import asyncio

    with pytest.raises(ValueError, match="gopher"):
        asyncio.run(UriFetcher().fetch("gopher://old/1.tif"))


# ── validation: packages/validate's first consumer ────────────────────────────────────


def test_an_empty_payload_is_refused_distinctly() -> None:
    """A 200 with no body is a SOURCE problem, not a corrupt file, and reads differently."""
    assert PayloadValidator().check("file:///a.tif", b"") == "empty payload"


def test_a_corrupt_tiff_is_refused_before_it_can_reach_bronze() -> None:
    """Bronze is the replay foundation: a corrupt page there is reproduced by every tier above it.

    Catching it at acquisition turns a read-time failure months later in someone else's job into a
    tracked error on the run that caused it.
    """
    reason = PayloadValidator().check("file:///pages/0001.tif", b"II*\x00 not really a tiff")

    assert reason is not None


def test_a_valid_png_passes() -> None:
    """The gate is for CORRUPTION, not a format allow-list — a good file must land.

    Encoded by PIL rather than pasted as a hex literal, so the fixture is a real image by
    construction. A hand-written one that happened to be malformed would pass this test for the
    wrong reason — it would prove the validator rejects things, which the corrupt case already
    proves.
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (120, 30, 30)).save(buffer, format="PNG")

    assert PayloadValidator().check("file:///a.png", buffer.getvalue()) is None


def test_an_unknown_extension_is_ACCEPTED() -> None:
    """Bronze is faithful to source (§3.5). Refusing an unrecognised format would make the plane
    reject new material for being new, which is the opposite of an archive's job."""
    assert PayloadValidator().check("file:///a.jp2", b"\x00\x00\x00\x0cjP  \r\n\x87\n") is None


def test_a_iiif_url_with_a_query_string_still_resolves_its_extension() -> None:
    """IIIF keys end `/full/max/0/default.jpg?x=1` — reading the query as part of the name would
    silently skip validation on every IIIF unit the plane ingests."""
    from ingest.validation import _extension

    assert _extension("https://lbiiif.riksarkivet.se/arkis!A0/1/full/max/0/default.jpg?q=1") == ".jpg"
