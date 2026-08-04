"""COCO instance annotations → the canonical Arrow the annotator imports.

This script is the OTHER half of the import design, and the reason the service has no parser in it.

`POST /tasks/{id}/import` accepts exactly one format: Arrow IPC matching `annotations/schema.py`.
Putting COCO, YOLO and Label Studio parsers inside the service would have made three things that
rot — each with its own edge cases, its own tests, and its own bug reports — all to produce something
the canonical schema already describes. Out here a converter is replaceable without touching a
service, testable on its own, and a new source format is a new file rather than a new service
dependency.

So this is a TEMPLATE as much as a tool. A YOLO or Label Studio converter is the same shape: read the
foreign file, emit the canonical schema, write IPC.

    uv run python scripts/coco_to_annotations.py instances.json --image 42 -o out.arrow
    curl -X POST --data-binary @out.arrow \\
         -H 'Content-Type: application/vnd.apache.arrow.stream' \\
         localhost:8103/tasks/<task-id>/import

One image at a time, on purpose: a task is one item, so an import that spanned images would land
annotations belonging to other items in this task's draft.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pyarrow as pa


#: The columns this converter emits, DECLARED rather than inferred.
#:
#: Load-bearing, and not obvious: `pa.Table.from_pylist` infers its schema from the FIRST row, so a
#: heterogeneous list of dicts silently drops every column row one happens not to carry. A COCO file
#: whose first annotation has no segmentation would lose `polygon` for every annotation after it,
#: with nothing raised anywhere. Naming the schema is what makes that impossible.
SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("shape_type", pa.string()),
        ("label", pa.string()),
        ("x", pa.float32()),
        ("y", pa.float32()),
        ("width", pa.float32()),
        ("height", pa.float32()),
        ("polygon", pa.list_(pa.float32())),
        ("attributes", pa.string()),
    ]
)


def rows_for_image(coco: dict[str, Any], image_id: int) -> list[dict[str, Any]]:
    """One COCO image's annotations as canonical rows.

    A segmentation becomes a `polygon`, otherwise the bbox becomes a `bbox` — COCO carries both and
    the polygon is the more specific claim, so it wins when present. COCO's multi-part segmentations
    (a list of rings, for an object split by an occluder) keep only the FIRST ring, because the
    canonical `polygon` is one ring; that is a real narrowing and it is reported on stderr rather
    than dropped quietly.
    """
    categories = {c["id"]: c["name"] for c in coco.get("categories", [])}
    rows: list[dict[str, Any]] = []
    for ann in coco.get("annotations", []):
        if ann.get("image_id") != image_id:
            continue

        segmentation = ann.get("segmentation") or []
        # RLE segmentations arrive as a dict, not a list of rings, and this converter does not decode
        # them. Falling through to the bbox is the honest answer — a wrong polygon would be worse.
        rings = segmentation if isinstance(segmentation, list) else []
        if len(rings) > 1:
            print(f"note: annotation {ann.get('id')} has {len(rings)} segmentation rings; keeping the first", file=sys.stderr)

        row: dict[str, Any] = {
            "id": f"coco-{ann.get('id')}",
            "label": categories.get(ann.get("category_id"), ""),
            "attributes": json.dumps({"iscrowd": str(ann.get("iscrowd", 0))}, separators=(",", ":")),
            "shape_type": "bbox",
            "x": None,
            "y": None,
            "width": None,
            "height": None,
            "polygon": None,
        }

        if rings and rings[0]:
            row["shape_type"] = "polygon"
            row["polygon"] = [float(v) for v in rings[0]]
        else:
            bbox = ann.get("bbox") or []
            if len(bbox) != 4:
                print(f"note: annotation {ann.get('id')} has neither a usable segmentation nor a bbox; skipped", file=sys.stderr)
                continue
            x, y, w, h = (float(v) for v in bbox)
            row.update({"x": x, "y": y, "width": w, "height": h})

        rows.append(row)
    return rows


def to_ipc(rows: list[dict[str, Any]]) -> bytes:
    """Canonical rows → Arrow IPC stream bytes, the payload the endpoint reads."""
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("coco", type=Path, help="a COCO instances JSON file")
    parser.add_argument("--image", type=int, required=True, help="the COCO image_id to convert (one task is one item)")
    parser.add_argument("-o", "--out", type=Path, required=True, help="where to write the Arrow IPC stream")
    args = parser.parse_args(argv)

    coco = json.loads(args.coco.read_text())
    rows = rows_for_image(coco, args.image)
    if not rows:
        # Not an error — an image with no annotations is a legitimate thing to ask about — but it
        # must be SAID, because an empty file posted to the endpoint is refused and the reason would
        # otherwise look like it came from the server.
        print(f"no annotations for image_id {args.image}; nothing written", file=sys.stderr)
        return 1

    args.out.write_bytes(to_ipc(rows))
    print(f"{len(rows)} annotations -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
