"""The ontology as a DECODE-TIME contract — JSON Schema for vLLM-style structured output.

The owner's observation, verbatim: the schema we already enforce is exactly what a constrained
decoder (Outlines/xgrammar behind vLLM's ``guided_json``) wants as input. These tests pin the
derivation: per (class, tool) branches whose consts make the branch choice BE the class+tool
choice, geometry that follows the tool, typed attributes with required enforced, transcription
present where declared — and honesty at the edges (no contract ⇒ ``None``, never a schema that
constrains nothing while reading as if it enforced something).

The schema is also validated by USE: the round-trip test runs a contract-shaped answer through
``jsonschema`` and an off-contract one is refused — the same judgement a constrained decoder
applies token-by-token.
"""

from __future__ import annotations

import jsonschema
import pytest
from annotator.projects.generation_schema import generation_schema
from annotator.projects.ontology import LabelClass, LabelOntology, OutputAttr


OCR = LabelOntology(
    kind="ocr-layout",
    classes=[
        LabelClass(
            name="paragraph",
            tools=["polygon", "bbox"],
            transcribe=True,
            attributes=[
                OutputAttr(name="order", type="int", required=True),
                OutputAttr(name="script", type="enum", choices=["blackletter", "cursive"]),
            ],
        ),
        LabelClass(name="person", tools=["text"]),
        LabelClass(name="damaged", tools=["tag"]),
    ],
)


def test_no_contract_means_NO_schema() -> None:
    """A schema fabricated from nothing would constrain to nothing while reading as enforcement."""
    assert generation_schema(None) is None
    assert generation_schema(LabelOntology(kind="x")) is None


def test_one_branch_per_class_and_tool_with_consts_pinning_the_choice() -> None:
    schema = generation_schema(OCR)
    assert schema is not None
    branches = schema["properties"]["annotations"]["items"]["anyOf"]
    picks = {(b["properties"]["label"]["const"], b["properties"]["shape_type"]["const"]) for b in branches}
    assert picks == {("paragraph", "polygon"), ("paragraph", "bbox"), ("person", "text"), ("damaged", "tag")}


def test_geometry_follows_the_tool() -> None:
    schema = generation_schema(OCR)
    assert schema is not None
    by_pick = {(b["properties"]["label"]["const"], b["properties"]["shape_type"]["const"]): b for b in schema["properties"]["annotations"]["items"]["anyOf"]}
    assert "polygon" in by_pick[("paragraph", "polygon")]["properties"]
    assert "x" in by_pick[("paragraph", "bbox")]["properties"]
    assert "char_start" in by_pick[("person", "text")]["properties"]
    # A tag has NO geometry — and additionalProperties: false makes emitting any impossible.
    tag = by_pick[("damaged", "tag")]
    assert set(tag["properties"]) == {"label", "shape_type"}
    assert tag["additionalProperties"] is False


def test_transcription_and_typed_attributes_ride_the_branch() -> None:
    schema = generation_schema(OCR)
    assert schema is not None
    para = next(
        b
        for b in schema["properties"]["annotations"]["items"]["anyOf"]
        if b["properties"]["label"]["const"] == "paragraph" and b["properties"]["shape_type"]["const"] == "bbox"
    )
    assert para["properties"]["text"] == {"type": "string"}
    attrs = para["properties"]["attributes"]
    assert attrs["properties"]["order"] == {"type": "integer"}
    assert attrs["properties"]["script"] == {"enum": ["blackletter", "cursive"]}
    assert attrs["required"] == ["order"]
    # A REQUIRED attribute makes the attributes object itself required on the branch.
    assert "attributes" in para["required"]


def test_allow_empty_mirrors_the_submit_rule() -> None:
    constrained = generation_schema(OCR)
    assert constrained is not None and constrained["properties"]["annotations"]["minItems"] == 1
    blank_ok = generation_schema(OCR.model_copy(update={"allow_empty": True}))
    assert blank_ok is not None and "minItems" not in blank_ok["properties"]["annotations"]


def test_the_schema_JUDGES_like_the_contract_does() -> None:
    """Validated by use: a contract-shaped answer passes, an off-contract one is refused — the
    same judgement a constrained decoder applies during generation."""
    schema = generation_schema(OCR)
    assert schema is not None
    good = {
        "annotations": [
            {
                "label": "paragraph",
                "shape_type": "bbox",
                "x": 10,
                "y": 10,
                "width": 200,
                "height": 80,
                "text": "Anno 1632",
                "attributes": {"order": 1, "script": "cursive"},
            },
            {"label": "damaged", "shape_type": "tag"},
        ]
    }
    jsonschema.validate(good, schema)

    for bad in (
        {"annotations": []},  # constrained ontology refuses an empty page
        {"annotations": [{"label": "seal", "shape_type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1}]},
        {"annotations": [{"label": "paragraph", "shape_type": "tag"}]},  # tool the class does not allow
        {
            "annotations": [{"label": "paragraph", "shape_type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1}]  # required attribute missing
        },
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


def test_an_unconstrained_class_may_generate_as_any_tool() -> None:
    schema = generation_schema(LabelOntology(classes=[LabelClass(name="free")]))
    assert schema is not None
    tools = {b["properties"]["shape_type"]["const"] for b in schema["properties"]["annotations"]["items"]["anyOf"]}
    assert {"bbox", "polygon", "text", "tag", "segment"} <= tools


def test_the_schema_rides_the_remote_wire() -> None:
    """A vLLM backend receives the contract IN the request — `output_schema` beside the prompt,
    ready for `guided_json`; a non-LLM backend is free to ignore the key."""
    from typing import Any

    from annotator.api.v1.endpoints.assist import AssistRequest, _remote

    from service_kit.media.state import AppState

    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict[str, Any]:
            return {"shapes": []}

    class _Http:
        def post(self, url: str, json: dict[str, Any], timeout: float) -> _Resp:
            captured.update(json)
            return _Resp()

    schema = generation_schema(OCR)
    _remote(AppState(http=_Http()), "http://model", ("d1", 0, 0), AssistRequest(producer="vlm"), schema)

    assert captured["output_schema"] == schema
