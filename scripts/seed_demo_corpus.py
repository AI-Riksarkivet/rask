#!/usr/bin/env python
"""Seed a minimal but REAL local media corpus, so the annotator canvas can be driven end-to-end.

`docs/OPEN-WORK.md` A1 records that the media corpus lives on a node-local `hostPath`, which means a
dev machine has no datasets and the annotator canvas has no page image to draw on. Everything the
registry needs is env-configurable though (`MEDIA_DB_ROOT` / `MEDIA_DESCRIPTOR_DIR`), so a corpus can
be synthesized locally instead — one document, one chunk, one real rendered page image.

This is a DEV FIXTURE, not production data. It exists so "witness the annotator in a browser" is a
command anyone can run rather than a claim gated on cluster access.

    uv run python scripts/seed_demo_corpus.py [root]

Then point the media services at it:

    MEDIA_DB_ROOT=<root> MEDIA_DESCRIPTOR_DIR=<root>/descriptors MEDIA_DB=demo.lance ...
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

import lance
import pyarrow as pa
from PIL import Image, ImageDraw


DOC_ID = "fe00cd746463ad2c"
DATASET_ID = "demo"


def page_image(width: int = 900, height: int = 1200) -> bytes:
    """Render a page-like image: ruled lines and blocks a bbox can plausibly enclose.

    Drawn rather than shipped as a binary so the fixture stays reviewable in a diff.
    """
    img = Image.new("RGB", (width, height), (250, 248, 242))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, width - 40, height - 40], outline=(200, 195, 185), width=2)
    draw.text((70, 70), "Riksarkivet — demo page (synthetic fixture)", fill=(60, 55, 50))
    y = 130
    for row in range(24):
        run = 760 if row % 4 != 3 else 420
        draw.rectangle([70, y, 70 + run, y + 18], fill=(70, 66, 62))
        y += 42
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def seed(root: Path) -> Path:
    db = root / f"{DATASET_ID}.lance"
    db.mkdir(parents=True, exist_ok=True)
    png = page_image()

    # The page image must be a Lance **blob-v2** column or the registry refuses the dataset
    # ("document.media_blob is not a lance.blob.v2 column"). Blob-v2 is a STRUCT — raw
    # large_binary is rejected outright — and it cannot be written at the default 2.1 file
    # format, hence data_storage_version="2.2".
    # speech_id/chunk_id are INTEGERS: the viewer builds its frame filter as
    # `speech_id = 0 AND chunk_id = 19` with unquoted numeric literals, so string columns
    # fail with "Received literal Int64(0) and could not convert to literal of type Utf8".
    blob = pa.struct([pa.field("data", pa.large_binary()), pa.field("uri", pa.utf8())])
    image_field = pa.field("image", blob, metadata={b"ARROW:extension:name": b"lance.blob.v2"})
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
        ]
    )
    chunks = pa.table(
        {
            "doc_id": pa.array([DOC_ID]),
            "speech_id": pa.array([0], pa.int64()),
            "chunk_id": pa.array([19], pa.int64()),
            "frame_idx": pa.array([0], pa.int64()),
            "image": pa.array([{"data": png, "uri": None}], type=blob),
            "mime": pa.array(["image/png"]),
            "caption": pa.array(["demo page 19"]),
        },
        schema=schema,
    )
    lance.write_dataset(chunks, str(db / "chunks.lance"), mode="overwrite", data_storage_version="2.2")

    documents = pa.table(
        {
            "doc_id": pa.array([DOC_ID]),
            "title": pa.array(["Demo volume"]),
            "n_chunks": pa.array([1], pa.int32()),
        }
    )
    lance.write_dataset(documents, str(db / "documents.lance"), mode="overwrite")

    descriptors = root / "descriptors"
    descriptors.mkdir(parents=True, exist_ok=True)
    # `capabilities` is DECLARED, not probed: it maps a capability name to the `table.column` it
    # needs, and `capability_available` just checks that column exists. Without the `frames` entry
    # the dataset lists with `capabilities: []` and the annotator has no page images to open.
    declared = {
        "identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"], "doc_key": "doc_id"},
        "document": {"table": "chunks", "media_blob": "image", "mime": "image/png"},
        "display": {"title": ["caption"], "caption": "caption"},
        "capabilities": {"frames": "chunks.image"},
    }
    (descriptors / f"{DATASET_ID}.json").write_text(json.dumps(declared, indent=2) + "\n")
    return db


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
    print(f"seeded {db}")
    print(f"descriptor: {root / 'descriptors' / f'{DATASET_ID}.json'}")


if __name__ == "__main__":
    main()
