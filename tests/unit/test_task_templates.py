"""Task templates v1 — the declarative output contract (the LS-config equivalent).

The template is CAPTURED onto every item at send and ENFORCED at submit in the task actor: the
same placement pattern as `review_required`, for the same reason — a client that could skip it
would publish items that do not carry what the task promised downstream. The default template is
unenforced, so every pre-template project behaves exactly as before.
"""

from __future__ import annotations

import pytest
from annotator.projects.models import OutputAttr, Shape, TaskTemplate, validate_against_template


def _shape(**kw: object) -> Shape:
    base: dict = {"shape_type": "bbox", "label": "portrait"}
    base.update(kw)
    return Shape.model_validate(base)


def test_the_default_template_enforces_nothing() -> None:
    """Backward compatibility is a contract: a template nobody wrote must not refuse anything."""
    assert validate_against_template(TaskTemplate(), [_shape(shape_type="text")]) is None
    assert validate_against_template(TaskTemplate(), []) is None


def test_a_disallowed_tool_is_named_in_the_violation() -> None:
    template = TaskTemplate(kind="classification", tools=["tag"], enforce=True)
    violation = validate_against_template(template, [_shape(shape_type="bbox")])
    assert violation is not None and "classification" in violation and "bbox" in violation


def test_missing_required_labels_refuse_with_the_missing_names() -> None:
    template = TaskTemplate(tools=["bbox"], required_labels=["portrait", "seal"], enforce=True)
    violation = validate_against_template(template, [_shape(label="portrait")])
    assert violation is not None and "seal" in violation


def test_an_empty_submission_fails_required_labels() -> None:
    """No draft ⇒ no shapes ⇒ the promised labels are absent — refused, not waved through."""
    template = TaskTemplate(required_labels=["portrait"], enforce=True)
    assert validate_against_template(template, []) is not None


@pytest.mark.parametrize(
    ("attr", "value", "ok"),
    [
        (OutputAttr(name="order", type="int", required=True), "3", True),
        (OutputAttr(name="order", type="int", required=True), "third", False),
        (OutputAttr(name="quality", type="enum", choices=["good", "bad"], required=True), "good", True),
        (OutputAttr(name="quality", type="enum", choices=["good", "bad"], required=True), "meh", False),
        (OutputAttr(name="note", type="free", required=True), "", False),
    ],
)
def test_typed_attributes_validate_by_type(attr: OutputAttr, value: str, ok: bool) -> None:
    template = TaskTemplate(tools=["bbox"], attributes=[attr], enforce=True)
    shapes = [_shape(attributes={attr.name: value} if value else {})]
    violation = validate_against_template(template, shapes)
    assert (violation is None) == ok, violation


def test_an_optional_attribute_may_be_absent() -> None:
    template = TaskTemplate(tools=["bbox"], attributes=[OutputAttr(name="note")], enforce=True)
    assert validate_against_template(template, [_shape()]) is None


# ------------------------------------------------------------------------------------------------
# The adversarial audit's confirmed findings, each pinned by the test that was missing
# ------------------------------------------------------------------------------------------------


def test_an_enforced_template_refuses_an_empty_submission() -> None:
    """The audit's critical: every other rule is a per-shape test, so zero shapes passed them all.

    Claim, submit, done — without drawing anything — on any enforced template whose
    `required_labels` is empty, which is exactly what the create dialog produces when the optional
    classes box is left blank."""
    template = TaskTemplate(kind="reading-order", tools=["bbox"], enforce=True)
    violation = validate_against_template(template, [])
    assert violation is not None and "at least one shape" in violation


def test_a_blank_item_stays_expressible_when_declared() -> None:
    """A blank page is a real archival outcome — it just has to be declared, not fallen into."""
    template = TaskTemplate(kind="reading-order", tools=["bbox"], enforce=True, allow_empty=True)
    assert validate_against_template(template, []) is None


@pytest.mark.parametrize(
    ("attr", "value", "needle"),
    [
        (OutputAttr(name="quality", type="enum", choices=["good", "bad"]), "GARBAGE", "must be one of"),
        (OutputAttr(name="order", type="int"), "third", "must be an integer"),
    ],
)
def test_an_optional_attribute_is_still_typed_when_present(attr: OutputAttr, value: str, needle: str) -> None:
    """`required` governs PRESENCE only. An optional `enum` still declares a closed set, and the
    facet publishes those `choices` as the column's advertised domain."""
    template = TaskTemplate(kind="classification", tools=["tag"], attributes=[attr], enforce=True)
    shape = _shape(shape_type="tag", attributes={attr.name: value})
    violation = validate_against_template(template, [shape])
    assert violation is not None and needle in violation
    assert validate_against_template(template, [_shape(shape_type="tag")]) is None


def test_a_mistyped_enforcement_key_is_refused_not_dropped() -> None:
    """`enforced` is not `enforce`. Under pydantic's default the key was dropped and the project
    created 201 with enforcement silently off — a contract advertised and never applied."""
    with pytest.raises(ValueError, match="enforced"):
        TaskTemplate.model_validate({"kind": "classification", "tools": ["tag"], "enforced": True})
    with pytest.raises(ValueError, match="require"):
        OutputAttr.model_validate({"name": "order", "type": "int", "require": True})
