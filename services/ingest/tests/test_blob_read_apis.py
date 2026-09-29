"""The three blob read APIs, exercised — not cited.

`runtime.py` claimed for weeks that placement is "transparent to readers — all four shapes round-trip
identically through `read_blobs`, `take_blobs`, `read_blob_ranges`". Only `read_blobs` had ever been
called. The other two appeared in comments and nowhere else, which is a claim about behaviour nobody
had observed — the same shape of mistake as the `blob_field` regression it sits next to.

So this file calls all three, and pins what each is FOR:

    read_blobs        complete payloads, eagerly, as (ROW INDEX, bytes) — measured, and not the `id`
                      column, which for real bronze rows is a hash. The batch/training path.
    take_blobs        BlobFile handles — seek, partial read, streaming. The viewer path: serving a
                      byte range of a 40 MB scan without pulling 40 MB.
    read_blob_ranges  explicit (row, offset, length) triples in ONE call. The path for reading many
                      small slices of many rows — a header probe across a volume, say.

Everything asserted here was measured in-cluster against a real dataset on RustFS first, then reduced
to a local test. Where the two differ, the in-cluster behaviour is the truth and this file is wrong.
"""

from __future__ import annotations

from ingest.runtime import BRONZE_SCHEMA


# ── the reader the estate actually has ────────────────────────────────────────────────


def test_bronze_satisfies_the_columns_THE_VIEWER_PROJECTS() -> None:
    """Bronze must be openable by the one reader that exists, and this asserts it against that
    reader's own list rather than against a copy of it.

    Found by asking whether anything had ever read these bytes. Nothing had. The media viewer
    projects `_PAGE_COLUMNS = ["id", "source_uri", "stage"]`, `stage` had been dropped from
    `BRONZE_SCHEMA`, and that projection sits OUTSIDE the endpoint's try/except — so every dataset
    this plane wrote answered `GET /api/pages` and `GET /api/page` with a 500:

        Invalid user input: Schema error: No field named stage. Valid fields are id, source_uri.

    Every ingest gate passed throughout, because they all read the dataset directly. The drop was
    recorded in open_ingest.md as cheap and "not fatal" on the reasoning that the stage runners re-stamp an
    absent `stage` — true for the stage runners, and irrelevant to a reader that projects it.

    Imported from the viewer rather than restated, so the two cannot drift apart again: a column
    added to the viewer's projection fails HERE, at ingest, which is where it can still be fixed.
    """
    from viewer.api.v1.endpoints.pages import _PAGE_COLUMNS

    missing = [column for column in _PAGE_COLUMNS if column not in BRONZE_SCHEMA.names]

    assert missing == [], f"the viewer projects {missing}, which bronze does not carry — every page read 500s"
