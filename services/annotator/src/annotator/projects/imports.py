"""Annotation import — bring labels made elsewhere into a task's draft.

ONE format: **Arrow IPC matching the annotations schema**. Not a parser zoo.

The tempting design was to accept COCO, YOLO and Label Studio JSON directly, and it is wrong. The
estate already HAS a canonical annotation format — `annotations/schema.py`'s `EMPTY_SCHEMA` — and it
is the contract the canvas reads, the draft validates and the publish writes. Three parsers inside
the service would be three things that rot, each with its own edge cases and its own tests, to
produce something the schema already describes. Converting COCO or YOLO into the canonical schema is
a ``scripts/`` concern: the repo's own convention for one-shot tooling, replaceable without touching
the service and testable on its own.

Bytes, not a table reference. The whole point is data that is NOT yet in the lakehouse — a governed
Lance table is a different and easier case that needs no import at all — so the transport is Arrow
IPC, matching the rule the estate already follows for bulk payloads.

**Where it lands, and why that is not the annotations table.** An imported label is unreviewed work,
and the draft is where unreviewed work lives. Landing it in the draft means it reaches the table
through the ordinary submit → accept → publish path, so an imported label earns exactly the same
provenance as a drawn one instead of appearing in the lakehouse having been seen by nobody.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from annotator.projects.models import Link, Shape
from annotator.projects.ontology import ShapeLike, membership_violation
from service_kit.exceptions import ValidationError


if TYPE_CHECKING:
    from annotator.projects.ontology import LabelOntology


#: Foreign shape names → the canonical vocabulary the draft, ontology and published table speak.
#: Even ONE format needs this: rows written by older tooling carry `rectangle`, a name neither the
#: service (`bbox`) nor the canvas accepts, and a row that silently keeps it is a row the canvas
#: cannot draw. Deliberately the same mapping the assist endpoint applies to producer output — the
#: normalisation is a property of the vocabulary, not of who is speaking it.
CANONICAL_SHAPE: dict[str, str] = {
    "rectangle": "bbox",
    "rect": "bbox",
    "box": "bbox",
    "bbox": "bbox",
    "polygon": "polygon",
    "mask": "mask",
    "point": "keypoint",
    "keypoint": "keypoint",
    "line": "polyline",
    "polyline": "polyline",
    "baseline": "polyline",
}

#: Columns read off an imported row. A row may carry the whole annotations schema — it is the
#: canonical format, so that is the normal case — and everything outside this set is IGNORED rather
#: than refused: `created_at`, `reviewer` and the identity key fields belong to the table the draft
#: is eventually written into, and are stamped there by the server, never accepted from a caller.
_TEXTUAL = ("label", "text", "mask", "group", "model_version", "parent_id")
_NUMERIC = ("x", "y", "width", "height", "rotation", "t_start", "t_end", "confidence")
_INTEGER = ("char_start", "char_end")

#: The one column an imported row cannot do without. Everything else has a defensible default; a row
#: with no shape type is not an annotation, it is a row.
_REQUIRED = "shape_type"

#: Stamped onto every imported shape. The draft vocabulary has no `status` column — `status` is an
#: ANNOTATIONS-table field, and these rows only acquire one when they are written there — so
#: "this was not drawn here" is carried by `source` plus `Draft.origin`, and the task's own state
#: machine is what guarantees the work is still reviewed. Import cannot skip submit or accept.
IMPORT_SOURCE = "import"


def _cell(row: dict[str, Any], name: str) -> Any:
    value = row.get(name)
    return None if value is None else value


def shapes_from_ipc(
    payload: bytes,
    *,
    ontology: LabelOntology | None = None,
    taken_ids: set[str] | None = None,
) -> tuple[list[Shape], list[Link]]:
    """Decode Arrow IPC into draft shapes + links, or raise `ValidationError` naming the offender.

    Fails CLOSED, and that is the deliberate difference from the assist endpoint beside it. Assist
    fails open — an unreadable rule must not lose a single interactive prediction the annotator is
    watching for. An import is a bulk write of somebody else's data that nobody is watching, so a
    label outside the taxonomy must be refused AT THE DOOR and named, not discovered at submit after
    a reviewer has already worked through the item.
    """
    table = _read_table(payload)
    taken = set(taken_ids or ())

    shapes: list[Shape] = []
    links: list[Link] = []
    for index, row in enumerate(table.to_pylist()):
        shape = _shape(row, index, taken)
        taken.add(shape.shape_id)
        shapes.append(shape)
        links.extend(_links(row, shape.shape_id, index))

    # The ontology check runs over the WHOLE set, after normalisation, because that is what the
    # canvas and the submit will see. Checking raw rows would refuse `rectangle` for a class whose
    # declared tool is `bbox` — a rejection caused by the importer's own vocabulary rather than by
    # anything wrong with the data.
    if ontology is not None:
        # Through `ShapeLike`, the same bridge the actor's submit check uses: the validator reads
        # id/type/label/attributes, and speaking to it in its own vocabulary is what keeps import and
        # submit judging by one implementation rather than two that agree until they don't.
        lookalikes = [ShapeLike.model_validate(s.model_dump(mode="json")) for s in shapes]
        violation = membership_violation(ontology, lookalikes)
        if violation is not None:
            raise ValidationError(f"import refused — {violation}")

        declared_relations = {r.name for r in ontology.relations}
        for link in links:
            if link.name not in declared_relations:
                raise ValidationError(f"import refused — relation {link.name!r} is not declared; known relations are {sorted(declared_relations)}")

    known = {s.shape_id for s in shapes} | set(taken_ids or ())
    for link in links:
        for end, shape_id in (("from", link.from_shape), ("to", link.to_shape)):
            if shape_id not in known:
                raise ValidationError(f"import refused — relation {link.name!r} points at unknown shape {shape_id!r} ({end})")

    return shapes, links


def _read_table(payload: bytes) -> pa.Table:
    """Arrow IPC bytes → a table, with a refusal a human can act on.

    Both framings are accepted because both are what `pyarrow` produces depending on which writer
    the caller reached for, and telling someone their valid Arrow file is invalid because they used
    `new_file` instead of `new_stream` is a distinction the format does not ask them to care about.
    """
    if not payload:
        raise ValidationError("import refused — the request carried no data")
    try:
        return pa.ipc.open_stream(pa.BufferReader(payload)).read_all()
    except pa.ArrowInvalid:
        pass
    try:
        return pa.ipc.open_file(pa.BufferReader(payload)).read_all()
    except pa.ArrowInvalid as exc:
        raise ValidationError(f"import refused — the payload is not Arrow IPC ({exc})") from exc


def _shape(row: dict[str, Any], index: int, taken: set[str]) -> Shape:
    raw_type = _cell(row, _REQUIRED)
    if raw_type is None or str(raw_type) == "":
        raise ValidationError(f"import refused — row {index} has no {_REQUIRED}")
    shape_type = CANONICAL_SHAPE.get(str(raw_type).lower(), str(raw_type).lower())

    # The canonical schema's identity column is `id`; the draft's is `shape_id`. Accept either, and
    # mint one when neither is present — an import with no ids is perfectly reasonable, it just
    # cannot carry links.
    supplied = _cell(row, "shape_id") or _cell(row, "id")
    shape_id = str(supplied) if supplied else None
    if shape_id is not None and shape_id in taken:
        # Re-keying silently would re-point any link that named it at a DIFFERENT annotation, which
        # is a wrong answer rather than a failed import. Refuse and name it.
        raise ValidationError(f"import refused — row {index} reuses shape id {shape_id!r}, which is already in this draft")

    fields: dict[str, Any] = {"shape_type": shape_type, "source": IMPORT_SOURCE}
    if shape_id is not None:
        fields["shape_id"] = shape_id
    for name in _TEXTUAL:
        value = _cell(row, name)
        if value is not None:
            fields[name] = str(value)
    for name in _NUMERIC:
        value = _cell(row, name)
        if value is not None:
            fields[name] = float(value)
    for name in _INTEGER:
        value = _cell(row, name)
        if value is not None:
            fields[name] = int(value)
    polygon = _cell(row, "polygon")
    if polygon is not None:
        fields["polygon"] = [float(v) for v in polygon]
    difficult = _cell(row, "difficult")
    if difficult is not None:
        fields["difficult"] = bool(difficult)
    fields["attributes"] = _attributes(row, index)

    try:
        return Shape.model_validate(fields)
    except Exception as exc:  # noqa: BLE001 - pydantic's message names the field; re-raise as a 422
        raise ValidationError(f"import refused — row {index} is not a valid annotation ({exc})") from exc


def _attributes(row: dict[str, Any], index: int) -> dict[str, str]:
    """`attributes` as the ontology means it: a flat string map.

    Values are stringified rather than refused when they arrive as numbers or booleans, because the
    ontology's own attribute types (`int`, `bool`, `enum`) are declared over string values and
    validated by parsing them — so an integer attribute written as an Arrow int is the SAME value,
    spelled the way its writer happened to spell it.
    """
    raw = _cell(row, "attributes")
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise ValidationError(f"import refused — row {index} has attributes that are not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ValidationError(f"import refused — row {index} has attributes that are not an object")
    return {str(k): ("" if v is None else str(v)) for k, v in raw.items()}


def _links(row: dict[str, Any], shape_id: str, index: int) -> list[Link]:
    """The row's `links` column → typed edges.

    `from_shape` defaults to the row that carries the link, which is what makes the column usable at
    all: a per-row JSON array most naturally reads as "this annotation's edges", and demanding the
    writer repeat the row's own id would be ceremony. A link that DOES name its source is honoured
    as written, so a file that carries full triples round-trips unchanged.
    """
    raw = _cell(row, "links")
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"import refused — row {index} has links that are not valid JSON ({exc})") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError(f"import refused — row {index} has links that are not an array")

    out: list[Link] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValidationError(f"import refused — row {index} has a link that is not an object")
        name, to_shape = entry.get("name"), entry.get("to_shape")
        if not name or not to_shape:
            raise ValidationError(f"import refused — row {index} has a link without a name and a to_shape")
        out.append(Link(name=str(name), from_shape=str(entry.get("from_shape") or shape_id), to_shape=str(to_shape)))
    return out
