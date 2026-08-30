"""What each labeling task's SAVED annotations actually look like — proven on the real write path.

The e2e suites stub the save POST, so a field the client sends and the server silently drops is
invisible to them: the canvas shows the annotation, the payload asserts clean, and the row lands
in Lance without the field. These tests close that hole by driving the same primitives the save
endpoint runs (``NewAnnotation`` validation → ``new_rows`` → a real Lance ``merge_insert``) and
reading the rows back, per task shape:

* detection (bbox/polygon geometry + class label),
* a model PREDICTION (status/source/scores — the review queue's input),
* item-level classification (`tag` rows),
* text spans (`parent_id`/`char_start`/`char_end` — token-classification's whole anchor),
* the composite OCR/HTR item (region classes + per-region transcription + a span + a page tag
  in ONE atomic version — the layout-plus-content task every template composes toward).

Plus the drift gate: every field the frontend's ``InsertRow`` sends must be DECLARED on
``NewAnnotation``. Pydantic ignores unknown fields by default, so an undeclared field is not an
error anywhere — it is a column that quietly lands null. That is exactly how text spans shipped
broken: readable, seedable, rendered — and dropped by every real save.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import lance
import pyarrow as pa
import pytest

from annotator.annotations.save import new_rows
from annotator.annotations.schema import EMPTY_SCHEMA, NewAnnotation
from annotator.api.v1.endpoints.assist import _CANONICAL_SHAPE, AssistShape, _within_contract


if TYPE_CHECKING:
    from pathlib import Path

#: The wire fields `makeInsertRow` sends on EVERY insert —
#: frontend/packages/labeling/src/annotations-client.ts `InsertRow`. Mirrored as a literal
#: because the two planes share no type system; this gate is the alignment.
INSERT_ROW_FIELDS = frozenset(
    {
        "id",
        "shape_type",
        "x",
        "y",
        "width",
        "height",
        "rotation",
        "polygon",
        "t_start",
        "t_end",
        "mask",
        "label",
        "text",
        "group",
        "status",
        "source",
        "parent_id",
        "char_start",
        "char_end",
        "confidence",
        "uncertainty",
        "metadata",
    }
)

_IDENT = {"doc_id": "d1", "speech_id": 0, "chunk_id": 19}


def _full_schema() -> pa.Schema:
    return pa.schema([("doc_id", pa.string()), ("speech_id", pa.int64()), ("chunk_id", pa.int64()), *EMPTY_SCHEMA])


def _save(tmp_path: Path, inserts: list[NewAnnotation]) -> list[dict]:
    """The save endpoint's write, minus HTTP: validated inserts → new_rows → merge_insert
    into a REAL Lance dataset → the rows read back. What these return IS what a reload serves."""
    uri = str(tmp_path / "annotations.lance")
    lance.write_dataset(_full_schema().empty_table(), uri)
    ds = lance.dataset(uri)
    ds.merge_insert("id").when_not_matched_insert_all().execute(new_rows(inserts, _IDENT, _full_schema()))
    return lance.dataset(uri).to_table().to_pylist()


def test_every_wire_field_the_client_sends_is_declared_on_the_insert_model() -> None:
    """The cross-plane drift gate. A field in `InsertRow` but not here is SILENTLY dropped."""
    declared = set(NewAnnotation.model_fields)
    missing = INSERT_ROW_FIELDS - declared
    assert not missing, f"InsertRow sends fields NewAnnotation silently drops: {sorted(missing)}"


def test_detection_row_saves_geometry_class_and_human_provenance(tmp_path: Path) -> None:
    rows = _save(
        tmp_path,
        [NewAnnotation(id="r1", shape_type="polygon", polygon=[10.0, 10.0, 90.0, 10.0, 90.0, 40.0], label="marginalia")],
    )
    (r,) = rows
    assert (r["doc_id"], r["speech_id"], r["chunk_id"]) == ("d1", 0, 19)
    assert r["shape_type"] == "polygon" and r["label"] == "marginalia"
    assert r["polygon"] == [10.0, 10.0, 90.0, 10.0, 90.0, 40.0]
    assert r["status"] == "accepted" and r["source"] == "human"
    assert r["confidence"] is None and r["uncertainty"] is None  # a person's shape has no score


def test_a_prediction_saves_the_scores_the_review_queue_ranks_by(tmp_path: Path) -> None:
    rows = _save(
        tmp_path,
        [
            NewAnnotation(
                id="p1",
                shape_type="bbox",
                x=5,
                y=5,
                width=50,
                height=20,
                label="stamp",
                status="prediction",
                source="model:grounding-dino",
                confidence=0.7,
                uncertainty=0.45,
            )
        ],
    )
    (r,) = rows
    assert r["status"] == "prediction" and r["source"] == "model:grounding-dino"
    assert r["confidence"] == pytest.approx(0.7) and r["uncertainty"] == pytest.approx(0.45)


def test_an_item_level_tag_row_is_an_ordinary_annotation(tmp_path: Path) -> None:
    rows = _save(tmp_path, [NewAnnotation(id="t1", shape_type="tag", label="damaged")])
    (r,) = rows
    assert r["shape_type"] == "tag" and r["label"] == "damaged"
    assert r["width"] == 0.0 and r["polygon"] == []  # a choice carries no geometry


def test_a_text_span_survives_the_save_path(tmp_path: Path) -> None:
    """The textual facet's anchor. Before `NewAnnotation` declared these three fields, this exact
    test read back nulls: the span rendered from the client's optimistic overlay, saved, and came
    back pointing at nothing."""
    rows = _save(
        tmp_path,
        [
            NewAnnotation(id="l1", shape_type="text", text="Gustaf Eriksson Vasa reste till Dalarna"),
            NewAnnotation(id="s1", shape_type="text", label="person", parent_id="l1", char_start=0, char_end=20),
        ],
    )
    by_id = {r["id"]: r for r in rows}
    span = by_id["s1"]
    assert span["parent_id"] == "l1"
    assert (span["char_start"], span["char_end"]) == (0, 20)
    # And a NON-span row keeps the sentinels, not a fake zero-length span at offset 0.
    line = by_id["l1"]
    assert line["parent_id"] == "" and line["char_start"] == -1 and line["char_end"] == -1


def test_the_composite_ocr_item_saves_as_one_coherent_version(tmp_path: Path) -> None:
    """The layout-plus-content task: region classes, a transcription ON a region, a span INTO that
    transcription, and an item-level tag — one save, one version, every relationship intact."""
    rows = _save(
        tmp_path,
        [
            NewAnnotation(id="hdr", shape_type="bbox", x=10, y=5, width=200, height=30, label="header"),
            NewAnnotation(
                id="par",
                shape_type="polygon",
                polygon=[10.0, 50.0, 210.0, 50.0, 210.0, 150.0, 10.0, 150.0],
                label="paragraph",
                text="för att samla allmogen mot unionskungen",
            ),
            NewAnnotation(id="ent", shape_type="text", label="person", parent_id="par", char_start=27, char_end=39),
            NewAnnotation(id="pg", shape_type="tag", label="damaged"),
        ],
    )
    by_id = {r["id"]: r for r in rows}
    assert len(rows) == 4
    assert by_id["hdr"]["label"] == "header" and by_id["hdr"]["shape_type"] == "bbox"
    # The transcription lives ON the region row — layout and content are one row, not two planes.
    assert by_id["par"]["label"] == "paragraph"
    assert by_id["par"]["text"] == "för att samla allmogen mot unionskungen"
    # The entity span anchors INTO the region's transcription by id + offsets.
    assert by_id["ent"]["parent_id"] == "par"
    assert by_id["par"]["text"][by_id["ent"]["char_start"] : by_id["ent"]["char_end"]] == "unionskungen"
    assert by_id["pg"]["shape_type"] == "tag"
    # Every row carries the SAME unit identity — the item stays one queryable thing.
    assert {(r["doc_id"], r["speech_id"], r["chunk_id"]) for r in rows} == {("d1", 0, 19)}


def test_per_row_attributes_ride_metadata_and_survive_an_edit_patch(tmp_path: Path) -> None:
    """The ontology's typed per-class attributes (`reading order`, `script`, …) live on the row as
    a JSON object of string values in `metadata` — the same flat map the submit validator parses.
    Both halves of the write path carry it: the insert, and the EDITABLE_FIELDS patch (the
    inspector's attribute editor is a field edit like any other)."""
    from annotator.annotations.save import build_delta

    rows = _save(
        tmp_path,
        [
            NewAnnotation(id="par", shape_type="polygon", label="paragraph", metadata='{"order": "2", "script": "cursive"}'),
        ],
    )
    import json

    (r,) = rows
    # Compared PARSED: the pa.json_() column stores JSONB and canonicalizes whitespace.
    assert json.loads(r["metadata"]) == {"order": "2", "script": "cursive"}

    # The edit patch: correcting the reading order is one EDITABLE_FIELDS field edit.
    uri = str(tmp_path / "annotations.lance")
    ds = lance.dataset(uri)
    delta = build_delta(ds.to_table(), {"par": {"metadata": '{"order": "1", "script": "cursive"}'}})
    ds.merge_insert("id").when_matched_update_all().execute(delta)
    (after,) = lance.dataset(uri).to_table().to_pylist()
    assert json.loads(after["metadata"]) == {"order": "1", "script": "cursive"}
    assert after["label"] == "paragraph"  # untouched fields carried forward


@pytest.mark.asyncio
async def test_assist_predictions_are_filtered_by_the_composite_ontology(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ML-backend interplay for a CUSTOM task: the server checks every producer shape against
    the task's own taxonomy — the union of per-class tools — before it reaches a human. A mask
    prediction against a polygon/bbox/text layout task is dropped AND reported, not queued."""

    class _Task:
        async def get(self) -> dict:
            return {
                "ontology": {
                    "kind": "ocr-layout",
                    "classes": [
                        {"name": "header", "tools": ["bbox"]},
                        {"name": "paragraph", "tools": ["polygon", "bbox"]},
                        {"name": "marginalia", "tools": ["polygon"]},
                        {"name": "transcription", "tools": ["text"]},
                    ],
                }
            }

    import annotator.api.v1.endpoints.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "_proxy", lambda _tid: _Task())

    def shape(t: str) -> AssistShape:
        return AssistShape(shape_type=t, x=0, y=0, width=10, height=10)

    # The route normalizes producer dialects BEFORE the filter (assist.py's canonicalization
    # loop) — mirrored here because `_within_contract` sees only canonical types in production.
    shapes = [shape("polygon"), shape("rectangle"), shape("mask")]
    for s in shapes:
        s.shape_type = _CANONICAL_SHAPE.get(s.shape_type, s.shape_type)

    from annotator.api.v1.endpoints.assist import _task_ontology

    kept, dropped = _within_contract(shapes, await _task_ontology("t1"))

    # `rectangle` (a producer dialect) normalized to `bbox` and accepted; `mask` refused.
    assert [s.shape_type for s in kept] == ["polygon", "bbox"]
    assert len(dropped) == 1 and "mask" in dropped[0]


def test_a_span_reanchors_through_the_span_edit_channel(tmp_path: Path) -> None:
    """The transcription-edit remap (§8d.1): offsets are not editable fields — `SpanEdit` is the
    one legal writer, and the patch lands like geometry/temporal do, everything else carried."""
    from annotator.annotations.save import build_delta
    from annotator.annotations.schema import SaveAnnotations, SpanEdit

    _save(
        tmp_path,
        [
            NewAnnotation(id="line", shape_type="text", text="Gustaf Eriksson Vasa"),
            NewAnnotation(id="span", shape_type="text", label="person", parent_id="line", char_start=16, char_end=20),
        ],
    )
    # The wire model carries the channel (a client edit of 'Eriksson' → 'Erikssonn' shifts +1).
    body = SaveAnnotations(spans=[SpanEdit(id="span", char_start=17, char_end=21)])
    edits_by_id: dict[str, dict[str, object]] = {}
    for s in body.spans:
        edits_by_id.setdefault(s.id, {}).update({"char_start": s.char_start, "char_end": s.char_end})

    uri = str(tmp_path / "annotations.lance")
    ds = lance.dataset(uri)
    ds.merge_insert("id").when_matched_update_all().execute(build_delta(ds.to_table(), edits_by_id))

    rows = {r["id"]: r for r in lance.dataset(uri).to_table().to_pylist()}
    assert (rows["span"]["char_start"], rows["span"]["char_end"]) == (17, 21)
    assert rows["span"]["parent_id"] == "line" and rows["span"]["label"] == "person"  # carried
