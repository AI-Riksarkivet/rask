"""Annotation import — ONE canonical format, landing in the DRAFT, refused against the ontology.

There was no way to get labels made elsewhere into rask. Work started in another tool had to be
redone by hand, which for a corpus of any size means it is not brought in at all.

The design decision worth restating, because the obvious alternative is worse: the endpoint accepts
**Arrow IPC matching the annotations schema** and nothing else. Accepting COCO, YOLO and Label
Studio JSON directly would put three parsers inside the service — three things that rot, each with
its own edge cases — to produce something `annotations/schema.py` already describes. Conversion is a
`scripts/` concern.

These tests are mostly about REFUSALS, because that is where an importer earns its keep: everything
it accepts, a reviewer will eventually have to look at.
"""

from __future__ import annotations

import io
import json

import pyarrow as pa
import pytest

from annotator.projects.imports import shapes_from_ipc
from annotator.projects.ontology import LabelClass, LabelOntology, RelationClass
from service_kit.exceptions import ValidationError


#: The annotator's default body cap (MEDIA_MAX_BODY_BYTES), far above every body here.
_BODY_LIMIT = 32 * 1024 * 1024


def ipc(rows: list[dict[str, object]], schema: pa.Schema | None = None) -> bytes:
    """Arrow IPC stream bytes, the way a `scripts/` converter would emit them."""
    table = pa.Table.from_pylist(rows, schema=schema) if schema else pa.Table.from_pylist(rows)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def ontology(**kw: object) -> LabelOntology:
    base: dict[str, object] = {
        "kind": "detection",
        "classes": [LabelClass(name="figure"), LabelClass(name="caption")],
        "allow_empty": True,
    }
    base.update(kw)
    return LabelOntology.model_validate(base)


# --------------------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------------------


def test_a_canonical_row_becomes_a_draft_shape() -> None:
    shapes, links = shapes_from_ipc(
        ipc([{"id": "a1", "shape_type": "bbox", "label": "figure", "x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}]),
        ontology=ontology(),
        max_bytes=_BODY_LIMIT,
    )

    assert len(shapes) == 1
    assert (shapes[0].shape_id, shapes[0].shape_type, shapes[0].label) == ("a1", "bbox", "figure")
    assert (shapes[0].x, shapes[0].y, shapes[0].width, shapes[0].height) == (1.0, 2.0, 3.0, 4.0)
    assert links == []


def test_a_row_with_no_id_still_imports_and_gets_one() -> None:
    """An export with no ids is perfectly reasonable — it just cannot carry links."""
    shapes, _ = shapes_from_ipc(ipc([{"shape_type": "bbox", "label": "figure"}]), ontology=ontology(), max_bytes=_BODY_LIMIT)

    assert shapes[0].shape_id, "an imported shape must always end up with an id"


@pytest.mark.parametrize(
    ("written", "canonical"),
    [("point", "keypoint")],
)
def test_a_foreign_shape_name_normalises(written: str, canonical: str) -> None:
    """Even ONE format needs this: rows written by older tooling carry `rectangle`, a name neither
    the service (`bbox`) nor the canvas accepts. A row that silently kept it is a row the canvas
    cannot draw."""
    shapes, _ = shapes_from_ipc(ipc([{"id": "a1", "shape_type": written}]), max_bytes=_BODY_LIMIT)

    assert shapes[0].shape_type == canonical


def test_normalisation_happens_BEFORE_the_ontology_check() -> None:
    """Otherwise the importer's own vocabulary causes the rejection.

    A class declaring the tool `bbox` must accept a row written as `rectangle` — they are the same
    shape. Checking raw rows would refuse it for a reason that has nothing to do with the data.
    """
    shapes, _ = shapes_from_ipc(
        ipc([{"id": "a1", "shape_type": "rectangle", "label": "figure"}]),
        ontology=ontology(classes=[LabelClass(name="figure", tools=["bbox"])]),
        max_bytes=_BODY_LIMIT,
    )

    assert shapes[0].shape_type == "bbox"


# --------------------------------------------------------------------------------------------------
# The ontology is the contract, and it is enforced AT IMPORT
# --------------------------------------------------------------------------------------------------


def test_ONE_bad_label_refuses_the_WHOLE_import() -> None:
    """All or nothing, on purpose.

    Importing the good rows and silently dropping the bad one would leave a draft that looks complete
    and is not — and the rows that vanished are exactly the ones someone needed to know about.
    """
    with pytest.raises(ValidationError):
        shapes_from_ipc(
            ipc(
                [
                    {"id": "a1", "shape_type": "bbox", "label": "figure"},
                    {"id": "a2", "shape_type": "bbox", "label": "spaceship"},
                ]
            ),
            ontology=ontology(),
            max_bytes=_BODY_LIMIT,
        )


def test_a_class_that_forbids_the_tool_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        shapes_from_ipc(
            ipc([{"id": "a1", "shape_type": "polygon", "label": "figure"}]),
            ontology=ontology(classes=[LabelClass(name="figure", tools=["bbox"])]),
            max_bytes=_BODY_LIMIT,
        )

    assert "figure" in str(caught.value)


def test_an_import_is_NOT_judged_by_the_completeness_rules() -> None:
    """The half of the contract that must NOT run here.

    `validate_against_ontology` also demands every REQUIRED class be present — correct at submit,
    wrong at import. An import is partial, unreviewed work by definition; refusing it because a
    required class has not been annotated yet refuses the very thing importing is for. The rule
    still runs at submit, where it means something.
    """
    shapes, _ = shapes_from_ipc(
        ipc([{"id": "a1", "shape_type": "bbox", "label": "figure"}]),
        ontology=ontology(classes=[LabelClass(name="figure"), LabelClass(name="caption", required=True)]),
        max_bytes=_BODY_LIMIT,
    )

    assert len(shapes) == 1


# --------------------------------------------------------------------------------------------------
# Relations
# --------------------------------------------------------------------------------------------------


def test_a_rows_links_column_becomes_typed_edges() -> None:
    """`from_shape` defaults to the row carrying the link — a per-row JSON array most naturally reads
    as "this annotation's edges", and making the writer repeat the row's own id is ceremony."""
    _, links = shapes_from_ipc(
        ipc(
            [
                {"id": "a1", "shape_type": "bbox", "label": "figure", "links": json.dumps([{"name": "describes", "to_shape": "a2"}])},
                {"id": "a2", "shape_type": "bbox", "label": "caption", "links": None},
            ]
        ),
        ontology=ontology(relations=[RelationClass(name="describes")]),
        max_bytes=_BODY_LIMIT,
    )

    assert len(links) == 1
    assert (links[0].name, links[0].from_shape, links[0].to_shape) == ("describes", "a1", "a2")


def test_an_undeclared_relation_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        shapes_from_ipc(
            ipc([{"id": "a1", "shape_type": "bbox", "label": "figure", "links": json.dumps([{"name": "haunts", "to_shape": "a1"}])}]),
            ontology=ontology(relations=[RelationClass(name="describes")]),
            max_bytes=_BODY_LIMIT,
        )

    assert "haunts" in str(caught.value)


def test_a_link_pointing_at_a_shape_that_is_not_there_is_refused() -> None:
    """A dangling edge is not a partial import, it is a wrong one: the canvas would draw a relation
    to nothing, and the submit check would refuse a draft the importer said was fine."""
    with pytest.raises(ValidationError) as caught:
        shapes_from_ipc(
            ipc([{"id": "a1", "shape_type": "bbox", "label": "figure", "links": json.dumps([{"name": "describes", "to_shape": "ghost"}])}]),
            ontology=ontology(relations=[RelationClass(name="describes")]),
            max_bytes=_BODY_LIMIT,
        )

    assert "ghost" in str(caught.value)


def test_a_link_may_point_at_a_shape_ALREADY_in_the_draft() -> None:
    """Import appends, so an imported edge onto existing work is legitimate — and would be refused as
    dangling if `taken_ids` were not consulted."""
    _, links = shapes_from_ipc(
        ipc([{"id": "a1", "shape_type": "bbox", "label": "figure", "links": json.dumps([{"name": "describes", "to_shape": "drawn-1"}])}]),
        ontology=ontology(relations=[RelationClass(name="describes")]),
        taken_ids={"drawn-1"},
        max_bytes=_BODY_LIMIT,
    )

    assert links[0].to_shape == "drawn-1"


# --------------------------------------------------------------------------------------------------
# Refusals that protect the draft
# --------------------------------------------------------------------------------------------------


def test_an_id_that_collides_with_the_existing_draft_is_refused() -> None:
    """Re-keying silently would re-point any link naming that id at a DIFFERENT annotation — a wrong
    answer rather than a failed import."""
    with pytest.raises(ValidationError) as caught:
        shapes_from_ipc(ipc([{"id": "drawn-1", "shape_type": "bbox"}]), taken_ids={"drawn-1"}, max_bytes=_BODY_LIMIT)

    assert "drawn-1" in str(caught.value)


def test_a_duplicate_id_WITHIN_one_import_is_refused() -> None:
    with pytest.raises(ValidationError):
        shapes_from_ipc(ipc([{"id": "a1", "shape_type": "bbox"}, {"id": "a1", "shape_type": "bbox"}]), max_bytes=_BODY_LIMIT)


def test_a_row_with_no_shape_type_is_refused() -> None:
    """It is not an annotation, it is a row."""
    with pytest.raises(ValidationError) as caught:
        shapes_from_ipc(ipc([{"id": "a1", "label": "figure"}]), max_bytes=_BODY_LIMIT)

    assert "shape_type" in str(caught.value)


def test_an_empty_body_is_refused() -> None:
    with pytest.raises(ValidationError):
        shapes_from_ipc(b"", max_bytes=_BODY_LIMIT)


def test_the_ARROW_FILE_framing_is_accepted_too() -> None:
    """`new_file` and `new_stream` are both what pyarrow produces depending on which writer the caller
    reached for. Telling someone their valid Arrow file is invalid is a distinction the format does
    not ask them to care about."""
    table = pa.Table.from_pylist([{"id": "a1", "shape_type": "bbox", "label": "figure"}])
    sink = io.BytesIO()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)

    shapes, _ = shapes_from_ipc(sink.getvalue(), ontology=ontology(), max_bytes=_BODY_LIMIT)

    assert shapes[0].shape_id == "a1"


# --------------------------------------------------------------------------------------------------
# Attributes
# --------------------------------------------------------------------------------------------------


def test_attributes_arrive_as_a_flat_string_map() -> None:
    shapes, _ = shapes_from_ipc(
        ipc([{"id": "a1", "shape_type": "bbox", "label": "figure", "attributes": json.dumps({"order": 2, "checked": True})}]),
        ontology=ontology(),
        max_bytes=_BODY_LIMIT,
    )

    assert shapes[0].attributes == {"order": "2", "checked": "True"}


def test_attributes_that_are_not_valid_json_are_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        shapes_from_ipc(ipc([{"id": "a1", "shape_type": "bbox", "attributes": "{oops"}]), max_bytes=_BODY_LIMIT)

    assert "attributes" in str(caught.value)


def test_a_declared_attribute_type_is_enforced_at_import() -> None:
    """The ontology's attribute typing is part of membership, so it runs here too — the same
    implementation the submit uses, which is the point of sharing it."""
    with pytest.raises(ValidationError) as caught:
        shapes_from_ipc(
            ipc([{"id": "a1", "shape_type": "bbox", "label": "figure", "attributes": json.dumps({"order": "not-a-number"})}]),
            ontology=ontology(classes=[LabelClass(name="figure", attributes=[{"name": "order", "type": "int"}])]),
            max_bytes=_BODY_LIMIT,
        )

    assert "order" in str(caught.value)


# --------------------------------------------------------------------------------------------------
# The text-span facet survives the trip
# --------------------------------------------------------------------------------------------------


#: The span columns, declared explicitly — see `test_a_text_span_imports_with_its_parent_and_range`.
_SPAN_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("shape_type", pa.string()),
        ("label", pa.string()),
        ("text", pa.string()),
        ("parent_id", pa.string()),
        ("char_start", pa.int32()),
        ("char_end", pa.int32()),
    ]
)


def test_a_text_span_imports_with_its_parent_and_range() -> None:
    """The textual facet survives the trip — and the schema must be DECLARED, not inferred.

    Written first with a bare `from_pylist`, this failed with `(None, None, None)`. Arrow infers the
    schema from the FIRST row, and the parent row carries no `parent_id` / `char_start` / `char_end`,
    so all three columns were dropped from the table before the importer ever saw them. Nothing
    errored — the same silent-loss shape as the endpoint that dropped `links`.

    It is pinned here rather than worked around because it is guidance for the `scripts/` converters
    that feed this endpoint: emit the canonical schema explicitly. A converter that lets Arrow infer
    from row one will silently drop every column its first annotation happens not to use.
    """
    shapes, _ = shapes_from_ipc(
        ipc(
            [
                {"id": "a1", "shape_type": "text", "label": "figure", "text": "Gustav Vasa"},
                {"id": "a2", "shape_type": "text", "label": "caption", "text": "Vasa", "parent_id": "a1", "char_start": 7, "char_end": 11},
            ],
            _SPAN_SCHEMA,
        ),
        ontology=ontology(),
        max_bytes=_BODY_LIMIT,
    )

    span = shapes[1]
    assert (span.parent_id, span.char_start, span.char_end) == ("a1", 7, 11)
