"""The ontology as a GENERATION contract — a JSON Schema for structured decoding.

The task's ontology already says everything a constrained decoder needs: which labels exist,
how each may be drawn, which typed fields it carries, whether its regions carry transcription.
Handed to a vLLM-style server's structured output (``guided_json`` — Outlines/xgrammar
underneath), the model *cannot* emit an off-contract annotation: the contract is enforced at
decode time, and the assist route's drop-and-report filter becomes belt-and-braces instead of
the only line of defence.

The derivation is deliberately per (class, tool) pair: ``label`` and ``shape_type`` are
``const`` in each branch, so the decoder's choice of branch IS the choice of class+tool, and
the geometry fields that follow are exactly the ones that tool needs — a bbox branch cannot
carry a polygon, a tag branch carries no geometry at all. ``additionalProperties: false``
everywhere, for the same reason the ontology itself is ``extra="forbid"``: a key the schema
does not name must be impossible, not ignored.
"""

from __future__ import annotations

from typing import Any

from annotator.projects.ontology import LabelClass, LabelOntology, OutputAttr, ShapeType


#: Geometry properties per canonical tool — the fields a generated annotation of that tool
#: must state. `mask` generates as a polygon on purpose: a language model realistically emits
#: vertices, not RLE, and the canonical-shape map already accepts that dialect.
_NUMBER = {"type": "number"}
_GEOMETRY: dict[str, dict[str, Any]] = {
    "bbox": {"x": _NUMBER, "y": _NUMBER, "width": _NUMBER, "height": _NUMBER},
    "polygon": {"polygon": {"type": "array", "items": _NUMBER, "minItems": 6}},
    "mask": {"polygon": {"type": "array", "items": _NUMBER, "minItems": 6}},
    "polyline": {"polygon": {"type": "array", "items": _NUMBER, "minItems": 4}},
    "keypoint": {"x": _NUMBER, "y": _NUMBER},
    "segment": {"t_start": _NUMBER, "t_end": _NUMBER},
    "text": {
        "char_start": {"type": "integer", "minimum": 0},
        "char_end": {"type": "integer", "minimum": 0},
    },
    "tag": {},
}

#: A class declaring NO tools is unconstrained — it may generate as any drawable tool.
_ANY_TOOL: tuple[ShapeType, ...] = ("bbox", "polygon", "mask", "keypoint", "polyline", "segment", "text", "tag")


def generation_schema(ontology: LabelOntology | None) -> dict[str, Any] | None:
    """The JSON Schema a constrained decoder enforces for this task — ``None`` when there is
    no contract to enforce (no ontology, or one that declares no classes), because a schema
    fabricated from nothing would CONSTRAIN to nothing and read as if it enforced something."""
    if ontology is None or not ontology.classes:
        return None
    branches = [_branch(c, tool) for c in ontology.classes for tool in (c.tools or _ANY_TOOL)]
    items: dict[str, Any] = {"anyOf": branches}
    annotations: dict[str, Any] = {"type": "array", "items": items}
    if not ontology.allow_empty:
        # The submit rule, mirrored at generation: a constrained ontology refuses an empty page
        # unless blankness was declared expressible.
        annotations["minItems"] = 1
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"annotations · {ontology.kind}" if ontology.kind else "annotations",
        "type": "object",
        "properties": {"annotations": annotations},
        "required": ["annotations"],
        "additionalProperties": False,
    }


def _branch(c: LabelClass, tool: str) -> dict[str, Any]:
    """One (class, tool) branch: consts pin the choice, then exactly that tool's geometry."""
    properties: dict[str, Any] = {
        "label": {"const": c.name},
        "shape_type": {"const": tool},
        **_GEOMETRY.get(tool, {}),
    }
    required = ["label", "shape_type", *_GEOMETRY.get(tool, {})]
    if c.transcribe:
        # The text facet — present, not required: a partially transcribed region is real work.
        properties["text"] = {"type": "string"}
    if c.attributes:
        attr_props = {a.name: _attr_schema(a) for a in c.attributes}
        attr_required = [a.name for a in c.attributes if a.required]
        properties["attributes"] = {
            "type": "object",
            "properties": attr_props,
            **({"required": attr_required} if attr_required else {}),
            "additionalProperties": False,
        }
        if attr_required:
            required.append("attributes")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _attr_schema(a: OutputAttr) -> dict[str, Any]:
    if a.type == "enum":
        return {"enum": list(a.choices)}
    if a.type == "int":
        return {"type": "integer"}
    if a.type == "bool":
        return {"type": "boolean"}
    return {"type": "string"}
