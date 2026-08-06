#!/usr/bin/env python
"""Seed a REAL, SEARCHABLE dev corpus, so every media surface shows something.

`OPEN-WORK.md` A1 records that the media corpus lives on a node-local `hostPath`, which means a
dev machine has no datasets: the annotator canvas has no page to draw on and search has nothing to
find. Everything the registry needs is env-configurable (`MEDIA_DB_ROOT` / `MEDIA_DESCRIPTOR_DIR`),
so the corpus can be synthesized locally instead.

**Why this grew from one row to a corpus.** The first version seeded ONE document with ONE chunk
and declared no `search` at all — so `/explorer` answered every query with zero hits and looked
broken while being "correct". A fixture whose whole purpose is "witness the estate working" has to
survive the first thing anyone does with it, which is type a word into the search box. So: several
documents, several chunks each, distinct prose per chunk, and an FTS index — a query for a common
word returns MANY hits across MANY documents.

This is a DEV FIXTURE, not production data. Every page image is drawn here rather than shipped as
a binary, so the fixture stays reviewable in a diff.

    uv run python scripts/seed_demo_corpus.py [root]

Then point the media services at it:

    MEDIA_DB_ROOT=<root> MEDIA_DESCRIPTOR_DIR=<root>/descriptors MEDIA_DB=demo.lance ...
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

import lance
import lancedb
import lancedb.index
import pyarrow as pa
from PIL import Image, ImageDraw


#: The dataset the fixture is written as. It must MATCH what the deployment asks for — the chart
#: points `MEDIA_DB` at `<root>/transcripts_v2.lance`, so a fixture named anything else makes every
#: media surface answer "dataset 'transcripts_v2' not found under /media-corpus" while the corpus
#: sits right there. `seed_dev_estate.sh` reads the running deployment and passes the name it
#: expects, so the fixture cannot drift from the config that reads it.
DATASET_ID = os.environ.get("SEED_DATASET_ID", "demo")

#: The fixture corpus: three volumes, each a handful of pages. The prose is deliberately
#: OVERLAPPING — every page mentions the archive and most mention a protokoll — so a one-word
#: query returns hits across several documents rather than a single row, and DIFFERENT enough that
#: a narrower query (e.g. "sjöfart") narrows the result set. That is the property that makes the
#: fixture able to demonstrate search at all.
DOCUMENTS: list[dict] = [
    {
        "doc_id": "fe00cd746463ad2c",
        "title": "Landskapshandlingar 1663",
        "pages": [
            "Protokoll hållet vid Riksarkivet den tredje maj, rörande jordeböcker och skatt.",
            "Vidare antecknades att kronans uppbörd av spannmål blivit fördröjd detta år.",
            "Domboken upptager tvist om ägogränser mellan två hemman i socknen.",
            "Avskrift av brev från landshövdingen angående vägarnas underhåll.",
        ],
    },
    {
        "doc_id": "a7419c0b2d5e8f31",
        "title": "Sjöfartshandlingar 1701",
        "pages": [
            "Protokoll rörande sjöfart och tullens uppbörd i hamnen under sommaren.",
            "Skeppslistan upptager fjorton fartyg, därav tre från främmande land.",
            "Anteckning om last av salt och järn, samt om besättningens rullor.",
        ],
    },
    {
        "doc_id": "b2c8e5417d90a6f4",
        "title": "Domboksregister 1688",
        "pages": [
            "Domboken för häradet upptager mål om arv, skuld och olovligt skogshygge.",
            "Protokoll från tinget: nämnden avkunnade dom i tvist om fiskevatten.",
            "Register över parter och vittnen, med hänvisning till Riksarkivet.",
        ],
    },
]


def page_image(title: str, page_no: int, width: int = 900, height: int = 1200) -> bytes:
    """Render a page-like image: ruled lines and blocks a bbox can plausibly enclose.

    Drawn rather than shipped as a binary so the fixture stays reviewable in a diff. The title and
    page number are stamped on so a screenshot of the canvas is self-identifying — otherwise every
    page looks identical and a drive cannot show it opened the page it meant to.
    """
    img = Image.new("RGB", (width, height), (250, 248, 242))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, width - 40, height - 40], outline=(200, 195, 185), width=2)
    draw.text((70, 70), f"{title} — page {page_no} (synthetic fixture)", fill=(60, 55, 50))
    y = 130
    for row in range(24):
        run = 760 if row % 4 != 3 else 420
        draw.rectangle([70, y, 70 + run, y + 18], fill=(70, 66, 62))
        y += 42
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _atlas_xy(doc_index: int, page_no: int) -> tuple[float, float]:
    """A STABLE 2-D position per chunk — one loose cluster per document.

    Not a real projection and not pretending to be: the fixture has no embedding model, and
    inventing one would make the atlas look meaningful when it is fixture data. What it must be is
    DETERMINISTIC (re-seeding cannot reshuffle the map under a saved lasso) and SEPARATED (pages of
    one volume land together, so a selection is visibly a selection). A document's angle around the
    unit circle plus a hashed jitter gives both, with no numpy dependency in a seeding script.
    """
    theta = 2.0 * math.pi * doc_index / max(len(DOCUMENTS), 1)
    digest = hashlib.sha256(f"{doc_index}:{page_no}".encode()).digest()
    jitter_x = (digest[0] / 255.0 - 0.5) * 0.45
    jitter_y = (digest[1] / 255.0 - 0.5) * 0.45
    return (math.cos(theta) + jitter_x, math.sin(theta) + jitter_y)


def _chunk_rows() -> dict[str, list]:
    """Flatten the fixture into columns. `speech_id` is the page and `chunk_id` its ordinal —
    the identity triple the whole media plane keys on."""
    keys = ("doc_id", "speech_id", "chunk_id", "frame_idx", "image", "mime", "caption", "text", "title", "atlas_x", "atlas_y", "atlas_cluster")
    columns: dict[str, list] = {k: [] for k in keys}
    for doc_index, document in enumerate(DOCUMENTS):
        for page_no, prose in enumerate(document["pages"]):
            columns["doc_id"].append(document["doc_id"])
            columns["speech_id"].append(page_no)
            columns["chunk_id"].append(page_no)
            columns["frame_idx"].append(0)
            columns["image"].append(page_image(document["title"], page_no))
            columns["mime"].append("image/png")
            columns["caption"].append(f"{document['title']} · page {page_no}")
            columns["text"].append(prose)
            columns["title"].append(document["title"])
            x, y = _atlas_xy(doc_index, page_no)
            columns["atlas_x"].append(x)
            columns["atlas_y"].append(y)
            columns["atlas_cluster"].append(doc_index)
    return columns


def seed(root: Path) -> Path:
    db = root / f"{DATASET_ID}.lance"
    # WIPE first. `mode="overwrite"` rewrites the data but leaves earlier `_indices/<uuid>`
    # directories behind, and a re-index can leave the dataset pointing at an index whose files
    # were collected — the reader then dies with `tokens.lance not found` and every query 400s
    # while the corpus looks perfectly healthy on disk. Re-seeding must be idempotent, so the
    # fixture is rebuilt from nothing rather than layered onto its own history.
    if db.exists():
        shutil.rmtree(db)
    db.mkdir(parents=True, exist_ok=True)

    # The page image must be a Lance **blob-v2** column or the registry refuses the dataset
    # ("document.media_blob is not a lance.blob.v2 column"). Blob-v2 is a STRUCT — raw
    # large_binary is rejected outright — and it cannot be written at the default 2.1 file
    # format, hence data_storage_version="2.2".
    # speech_id/chunk_id are INTEGERS: the viewer builds its frame filter as
    # `speech_id = 0 AND chunk_id = 19` with unquoted numeric literals, so string columns
    # fail with "Received literal Int64(0) and could not convert to literal of type Utf8".
    # `lance.blob_field` rather than a hand-rolled struct + `ARROW:extension:name` metadata. This was
    # the ONLY site in the repo spelling blob-v2 by hand; every other writer uses the helpers
    # (`medallion/services/ingest.py:20,48,86`, `ingest/runtime.py:24,128`). The two are not
    # equivalent: the helper yields a real Arrow EXTENSION type (`extension<lance.blob.v2<BlobType>>`),
    # while the hand-rolled struct only carries the name as field metadata — which any process that
    # has imported lance will fail to reconstruct across an Arrow-IPC boundary. It reads the same
    # today and is a landmine for every door this fixture is about to be put through.
    image_field = lance.blob_field("image")
    schema = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("speech_id", pa.int64()),
            pa.field("chunk_id", pa.int64()),
            # The frames capability selects a frame per chunk by `frame_idx` (0 = representative);
            # the endpoint projects this column, so it must exist even for a single-frame chunk.
            pa.field("frame_idx", pa.int64()),
            image_field,
            pa.field("mime", pa.utf8()),
            pa.field("caption", pa.utf8()),
            # The SEARCHABLE body and its document title — without these the dataset can be
            # browsed but never found, which is the failure this fixture exists to prevent.
            pa.field("text", pa.utf8()),
            pa.field("title", pa.utf8()),
            # The ATLAS projection. `AtlasSpace` binds x/y/cluster to real COLUMNS — it wants a
            # projection, not raw embeddings — so a fixture without them left `/api/explorer/atlas/
            # points` answering 404 and the atlas panel showing "this dataset has no embedding map"
            # forever. That made the whole atlas surface, and any dock panel over it, impossible to
            # exercise in the dev estate.
            pa.field("atlas_x", pa.float32()),
            pa.field("atlas_y", pa.float32()),
            pa.field("atlas_cluster", pa.int32()),
        ]
    )
    columns = _chunk_rows()
    chunks = pa.table(
        {
            "doc_id": pa.array(columns["doc_id"]),
            "speech_id": pa.array(columns["speech_id"], pa.int64()),
            "chunk_id": pa.array(columns["chunk_id"], pa.int64()),
            "frame_idx": pa.array(columns["frame_idx"], pa.int64()),
            "image": lance.blob_array(columns["image"]),
            "mime": pa.array(columns["mime"]),
            "caption": pa.array(columns["caption"]),
            "text": pa.array(columns["text"]),
            "title": pa.array(columns["title"]),
            "atlas_x": pa.array(columns["atlas_x"], pa.float32()),
            "atlas_y": pa.array(columns["atlas_y"], pa.float32()),
            "atlas_cluster": pa.array(columns["atlas_cluster"], pa.int32()),
        },
        schema=schema,
    )
    lance.write_dataset(chunks, str(db / "chunks.lance"), mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)

    # The doc-level table `document.table` points at — ONE row per document, and it carries its own
    # cover blob because `document.media_blob` must name a column in THIS table (the descriptor
    # validator refuses the dataset outright otherwise: "document.media_blob 'image' missing").
    # The cover is page 0, which is what a gallery thumbnail should show.
    doc_schema = pa.schema(
        [
            pa.field("doc_id", pa.utf8()),
            pa.field("title", pa.utf8()),
            pa.field("n_chunks", pa.int32()),
            # The SAME blob-v2 helper the chunks table uses — a bare struct is refused with
            # "document.media_blob 'image' is not a lance.blob.v2 column".
            lance.blob_field("image"),
            pa.field("mime", pa.utf8()),
        ]
    )
    documents = pa.table(
        {
            "doc_id": pa.array([d["doc_id"] for d in DOCUMENTS]),
            "title": pa.array([d["title"] for d in DOCUMENTS]),
            "n_chunks": pa.array([len(d["pages"]) for d in DOCUMENTS], pa.int32()),
            "image": lance.blob_array([page_image(d["title"], 0) for d in DOCUMENTS]),
            "mime": pa.array(["image/png"] * len(DOCUMENTS)),
        },
        schema=doc_schema,
    )
    lance.write_dataset(documents, str(db / "documents.lance"), mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)

    # The FTS index, through the LANCEDB api — the search service opens the row table with
    # `lancedb` and runs `tbl.search(MatchQuery(...), query_type="fts")`, which needs an index
    # built by that same stack. Writing the column without indexing it yields zero hits for every
    # query, indistinguishable from an empty corpus.
    connection = lancedb.connect(str(db))
    connection.open_table("chunks").create_index("text", config=lancedb.index.FTS(), replace=True)

    descriptors = root / "descriptors"
    descriptors.mkdir(parents=True, exist_ok=True)
    # `capabilities` is DECLARED, not probed: it maps a capability name to the `table.column` it
    # needs, and `capability_available` just checks that column exists. Without the `frames` entry
    # the dataset lists with `capabilities: []` and the annotator has no page images to open.
    # `search` is the same shape of declaration — absent, the search plane has no row table and
    # answers every query with nothing.
    declared = {
        "identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"], "doc_key": "doc_id"},
        # `document.table` is the DOC-LEVEL table: `/api/documents` pages it row-for-row with no
        # DISTINCT (viewer/api/v1/endpoints/system.py:151), so it must hold exactly one row per
        # document. Pointing it at the page-level `chunks` made the browse gallery list the same
        # doc_id four times, and the media zone keys that list by doc id — so Svelte threw
        # `each_key_duplicate` and the whole route died client-side. The blob binding stays on the
        # page-level image via `capabilities.frames` below.
        "document": {"table": "documents", "media_blob": "image", "mime": "image/png"},
        # `metadata` is not decoration: `/api/documents` projects exactly `doc_key + these fields`,
        # so a title declared ONLY under `display.title` comes back null and the browse gallery
        # renders three untitled cards.
        "display": {
            "title": ["title"],
            "body": "text",
            "caption": "caption",
            "metadata": [{"field": "title", "label": "Title"}, {"field": "n_chunks", "label": "Pages"}],
        },
        "search": {"row_table": "chunks", "fts": {"table": "chunks", "column": "text"}, "filterable": ["doc_id"]},
        # The ATLAS space. `x`/`y`/`cluster` name COLUMNS on `table` (descriptor.py:210-214
        # validates each one exists), so this declaration and the three seeded columns must land
        # together — declaring it over a table without them fails validation rather than degrading.
        # `source_column` is the text the atlas shows on hover; the channel colours points by
        # volume, which is what makes a lasso selection legible as a selection.
        "atlas": [
            {
                "name": "text",
                "table": "chunks",
                "x": "atlas_x",
                "y": "atlas_y",
                "cluster": "atlas_cluster",
                "source_column": "text",
                "channels": [{"name": "volume", "column": "title"}],
            }
        ],
        "capabilities": {"frames": "chunks.image"},
    }
    (descriptors / f"{DATASET_ID}.json").write_text(json.dumps(declared, indent=2) + "\n")
    _make_world_readable(root)
    return db


def _make_world_readable(root: Path) -> None:
    """Make the fixture readable by WHOEVER serves it — the whole point of a shared corpus.

    Lance writes with the process umask (0600 files here), and the media services run as uid 10001
    while a corpus is seeded by a developer or a Job under some other uid. The reader then gets
    EACCES, which Lance's object-store layer surfaces as **"Object at location …/tokens.lance not
    found"** — a not-found error for a file that is right there. That misdirection cost a long
    debugging detour: the index looked corrupt or version-skewed when it was merely unreadable.
    """
    # The CONTENTS are ours and must be readable — a failure here is the real defect and still raises.
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)

    # `root` itself is usually NOT ours. Seeding into a PersistentVolumeClaim mounts a root-owned
    # directory and the Job runs as uid 1000, so this chmod raises EPERM — after every dataset has
    # been written correctly. The seed then reports failure for a corpus that is complete and
    # readable, which is a worse lie than the permission it was trying to fix. The mount root's mode
    # is the volume's business (fsGroup/mountOptions), so it is best-effort with a visible warning.
    try:
        root.chmod(0o755)
    except PermissionError:
        print(f"  note: left {root} as-is (not owned by this process — normal on a mounted volume)")


def main() -> None:
    # Default under the system temp dir: this is a throwaway dev fixture, and writing it into the
    # repo would put a Lance dataset in git. `tempfile.gettempdir()` rather than a literal /tmp so
    # ruff's S108 stays satisfied and TMPDIR is honoured.
    default = Path(tempfile.gettempdir()) / "rask-demo-corpus"
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else default
    db = seed(root)
    for name in ("chunks", "documents"):
        ds = lance.dataset(str(db / f"{name}.lance"))
        print(f"  {name}.lance: {ds.count_rows()} rows, v{ds.version}")
    print(f"  fts index on chunks.text (searchable: {sum(len(d['pages']) for d in DOCUMENTS)} pages across {len(DOCUMENTS)} documents)")
    print(f"seeded {db}")
    print(f"descriptor: {root / 'descriptors' / f'{DATASET_ID}.json'}")


if __name__ == "__main__":
    main()
