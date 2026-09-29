"""The COCO converter, and the round trip that proves the import design holds.

The point of these is not COCO. It is that a `scripts/` converter can produce something the service
accepts WITHOUT the service knowing COCO exists — which is the whole argument for keeping format
conversion out of the service. If this round trip works, a YOLO converter is a new file and nothing
else changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from annotator.projects.imports import shapes_from_ipc
from annotator.projects.ontology import LabelClass, LabelOntology


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from coco_to_annotations import main, rows_for_image, to_ipc  # ty: ignore[unresolved-import] — sys.path above  # noqa: E402


#: The annotator's default body cap (MEDIA_MAX_BODY_BYTES), far above every body here.
_BODY_LIMIT = 32 * 1024 * 1024


COCO = {
    "categories": [{"id": 1, "name": "figure"}, {"id": 2, "name": "caption"}],
    "images": [{"id": 42}, {"id": 43}],
    "annotations": [
        {"id": 1, "image_id": 42, "category_id": 1, "bbox": [10, 20, 30, 40], "iscrowd": 0},
        {"id": 2, "image_id": 42, "category_id": 2, "segmentation": [[0, 0, 5, 0, 5, 5]], "bbox": [0, 0, 5, 5]},
        {"id": 3, "image_id": 43, "category_id": 1, "bbox": [1, 1, 1, 1]},
    ],
}


def test_an_RLE_segmentation_falls_back_to_the_bbox_rather_than_guessing() -> None:
    """RLE arrives as a dict, not a list of rings, and this converter does not decode it. A wrong
    polygon would be worse than a correct box."""
    coco = {
        "categories": [{"id": 1, "name": "figure"}],
        "annotations": [{"id": 9, "image_id": 1, "category_id": 1, "segmentation": {"counts": "abc", "size": [10, 10]}, "bbox": [1, 2, 3, 4]}],
    }

    rows = rows_for_image(coco, 1)

    assert rows[0]["shape_type"] == "bbox"


def test_an_annotation_with_neither_is_SKIPPED_not_emitted_broken() -> None:
    coco = {"categories": [{"id": 1, "name": "figure"}], "annotations": [{"id": 9, "image_id": 1, "category_id": 1}]}

    assert rows_for_image(coco, 1) == []


# --------------------------------------------------------------------------------------------------
# The round trip — a scripts/ converter feeding the service that has never heard of COCO
# --------------------------------------------------------------------------------------------------


def test_the_converters_output_imports_cleanly() -> None:
    shapes, links = shapes_from_ipc(
        to_ipc(rows_for_image(COCO, 42)),
        ontology=LabelOntology.model_validate({"kind": "detection", "classes": [LabelClass(name="figure"), LabelClass(name="caption")], "allow_empty": True}),
        max_bytes=_BODY_LIMIT,
    )

    assert [s.shape_id for s in shapes] == ["coco-1", "coco-2"]
    assert [s.shape_type for s in shapes] == ["bbox", "polygon"]
    assert [s.label for s in shapes] == ["figure", "caption"]
    assert links == []


def test_the_cli_writes_a_file_the_importer_reads(tmp_path: Path) -> None:
    src, out = tmp_path / "instances.json", tmp_path / "out.arrow"
    src.write_text(json.dumps(COCO))

    assert main([str(src), "--image", "42", "-o", str(out)]) == 0

    shapes, _ = shapes_from_ipc(out.read_bytes(), max_bytes=_BODY_LIMIT)
    assert len(shapes) == 2


def test_an_image_with_no_annotations_exits_nonzero_and_writes_nothing(tmp_path: Path) -> None:
    """An empty Arrow file posted to the endpoint is refused, and the reason would look like it came
    from the server. Better to fail here, where the cause is visible."""
    src, out = tmp_path / "instances.json", tmp_path / "out.arrow"
    src.write_text(json.dumps(COCO))

    assert main([str(src), "--image", "999", "-o", str(out)]) == 1
    assert not out.exists()
