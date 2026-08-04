"""The unified labeling ontology — one object that cannot contradict itself.

The model this replaces was two objects (`label_schema` + `template`) that no code path
cross-checked. These tests pin the properties that absence cost us: a taxonomy that is actually
closed, a tool restriction that cannot disagree with the classes it constrains, and relations —
without which KIE and DocVQA are not expressible at all, at any level of attribute typing.
"""

from __future__ import annotations

import pytest
from annotator.projects.ontology import (
    LabelClass,
    LabelOntology,
    LinkLike,
    OutputAttr,
    RelationClass,
    ShapeLike,
    validate_against_ontology,
)


def _shape(shape_id: str = "s1", shape_type: str = "bbox", label: str | None = None, **attrs: str) -> ShapeLike:
    return ShapeLike(shape_id=shape_id, shape_type=shape_type, label=label, attributes=dict(attrs))


# ── the contradiction the two-object model allowed ────────────────────────────────────────────────


def test_tools_are_DERIVED_from_the_taxonomy_so_they_cannot_disagree() -> None:
    """The old model let you pick `Shape kind = polygon` AND `Task type = bbox-detection`, creating a
    project whose taxonomy said polygon and whose enforcement refused every polygon. Here `tools` is
    a property of the classes, so there is no second place for it to disagree with."""
    ontology = LabelOntology(
        classes=[
            LabelClass(name="region", tools=["polygon"]),
            LabelClass(name="stamp", tools=["bbox"]),
        ]
    )
    assert ontology.tools == ["polygon", "bbox"]


def test_one_unconstrained_class_makes_the_task_unconstrained() -> None:
    """A class with no declared tools means "any" — so the derived union must not pretend the task is
    restricted to whatever the OTHER classes happen to declare."""
    ontology = LabelOntology(classes=[LabelClass(name="a", tools=["bbox"]), LabelClass(name="b")])
    assert ontology.tools == []


# ── the closed set: the one property a class list exists for ──────────────────────────────────────


def test_a_label_outside_the_taxonomy_is_refused() -> None:
    """Structurally impossible before: the send capture copied the template onto the task and NOT the
    taxonomy, so the class list was not even in scope where enforcement ran. `label='asdf'` published."""
    ontology = LabelOntology(classes=[LabelClass(name="portrait")])
    violation = validate_against_ontology(ontology, [_shape(label="asdf")])
    assert violation is not None
    assert "asdf" in violation and "portrait" in violation


def test_an_unlabelled_shape_is_not_forced_into_the_taxonomy() -> None:
    """Drawing before choosing a class is a normal intermediate state, not a violation."""
    ontology = LabelOntology(classes=[LabelClass(name="portrait")])
    assert validate_against_ontology(ontology, [_shape(label=None)]) is None


def test_tools_are_enforced_PER_CLASS_not_per_task() -> None:
    ontology = LabelOntology(classes=[LabelClass(name="portrait", tools=["bbox"])])
    violation = validate_against_ontology(ontology, [_shape(shape_type="polygon", label="portrait")])
    assert violation is not None and "portrait" in violation and "polygon" in violation


# ── required is per class, and explicit ───────────────────────────────────────────────────────────


def test_required_is_per_class_and_never_derived_from_the_class_list() -> None:
    """The create dialog used to set `required_labels` to EVERY class name, so adding a third class
    silently made all three mandatory on every item. Being mandatory is now its own per-class flag."""
    ontology = LabelOntology(classes=[LabelClass(name="portrait", required=True), LabelClass(name="seal")])
    assert validate_against_ontology(ontology, [_shape(label="portrait")]) is None

    violation = validate_against_ontology(ontology, [_shape(label="seal")])
    assert violation is not None and "portrait" in violation


def test_a_constrained_ontology_refuses_an_empty_submission() -> None:
    ontology = LabelOntology(kind="object-detection", classes=[LabelClass(name="portrait")])
    violation = validate_against_ontology(ontology, [])
    assert violation is not None and "at least one annotation" in violation


def test_a_blank_page_stays_expressible_when_declared() -> None:
    ontology = LabelOntology(classes=[LabelClass(name="portrait")], allow_empty=True)
    assert validate_against_ontology(ontology, []) is None


def test_an_empty_ontology_constrains_nothing() -> None:
    """No `enforce` flag: an ontology with nothing in it IS the unconstrained case, so no declaration
    can be silently voided by a boolean."""
    assert LabelOntology().constrains is False
    assert validate_against_ontology(LabelOntology(), [_shape(shape_type="text", label="anything")]) is None


# ── attributes belong to a class ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("attr", "value", "needle"),
    [
        (OutputAttr(name="order", type="int"), "third", "must be an integer"),
        (OutputAttr(name="q", type="enum", choices=["yes", "no"]), "maybe", "must be one of"),
        (OutputAttr(name="ok", type="bool"), "perhaps", "must be a boolean"),
    ],
)
def test_a_present_attribute_is_typed_even_when_optional(attr: OutputAttr, value: str, needle: str) -> None:
    """`required` governs PRESENCE only — a declared type applies to whatever is actually there."""
    ontology = LabelOntology(classes=[LabelClass(name="c", attributes=[attr])])
    violation = validate_against_ontology(ontology, [_shape(label="c", **{attr.name: value})])
    assert violation is not None and needle in violation
    assert validate_against_ontology(ontology, [_shape(label="c")]) is None


def test_choices_on_a_non_enum_is_refused_at_construction() -> None:
    """Declaring a closed set on a field that will never check it advertises a contract nothing applies."""
    with pytest.raises(ValueError, match="choices"):
        OutputAttr(name="x", type="free", choices=["a"])
    with pytest.raises(ValueError, match="choices"):
        OutputAttr(name="x", type="enum")


# ── relations: KIE and DocVQA, which were previously inexpressible ────────────────────────────────


def _kie() -> LabelOntology:
    """Key information extraction: the task is the LINK, not the boxes."""
    return LabelOntology(
        kind="token-classification",
        classes=[LabelClass(name="key", tools=["bbox"]), LabelClass(name="value", tools=["bbox"])],
        relations=[RelationClass(name="key-value", from_classes=["key"], to_classes=["value"], required=True)],
    )


def test_KIE_is_expressible_and_its_link_is_validated() -> None:
    ontology = _kie()
    shapes = [_shape("k1", label="key"), _shape("v1", label="value")]
    assert validate_against_ontology(ontology, shapes, [LinkLike(name="key-value", from_shape="k1", to_shape="v1")]) is None


def test_a_KIE_link_pointing_the_wrong_way_is_refused() -> None:
    """value→key is not key→value. Without declared endpoints this could not be checked at all."""
    ontology = _kie()
    shapes = [_shape("k1", label="key"), _shape("v1", label="value")]
    violation = validate_against_ontology(ontology, shapes, [LinkLike(name="key-value", from_shape="v1", to_shape="k1")])
    assert violation is not None and "source" in violation


def test_a_required_relation_missing_entirely_is_refused() -> None:
    ontology = _kie()
    shapes = [_shape("k1", label="key"), _shape("v1", label="value")]
    violation = validate_against_ontology(ontology, shapes, [])
    assert violation is not None and "key-value" in violation


def test_a_link_to_an_unknown_shape_is_refused() -> None:
    ontology = _kie()
    shapes = [_shape("k1", label="key"), _shape("v1", label="value")]
    violation = validate_against_ontology(ontology, shapes, [LinkLike(name="key-value", from_shape="k1", to_shape="ghost")])
    assert violation is not None and "ghost" in violation


def test_an_undeclared_relation_is_refused() -> None:
    ontology = _kie()
    shapes = [_shape("k1", label="key"), _shape("v1", label="value")]
    violation = validate_against_ontology(ontology, shapes, [LinkLike(name="invented", from_shape="k1", to_shape="v1")])
    assert violation is not None and "invented" in violation


def test_DocVQA_binds_a_question_to_the_region_that_answers_it() -> None:
    ontology = LabelOntology(
        kind="document-question-answering",
        classes=[
            LabelClass(name="question", tools=["text"], attributes=[OutputAttr(name="text", required=True)]),
            LabelClass(name="answer", tools=["bbox"]),
        ],
        relations=[RelationClass(name="answers", from_classes=["answer"], to_classes=["question"], required=True)],
    )
    shapes = [
        _shape("q1", shape_type="text", label="question", text="Vem avkunnade domen?"),
        _shape("a1", shape_type="bbox", label="answer"),
    ]
    assert validate_against_ontology(ontology, shapes, [LinkLike(name="answers", from_shape="a1", to_shape="q1")]) is None


# ── the ontology polices itself at construction ───────────────────────────────────────────────────


def test_a_relation_referencing_an_undeclared_class_is_refused_at_construction() -> None:
    """One object can still contradict itself unless something reads all of it. That happens in the
    model validator, so the create endpoint gets the check for free."""
    with pytest.raises(ValueError, match="undeclared classes"):
        LabelOntology(
            classes=[LabelClass(name="key")],
            relations=[RelationClass(name="kv", from_classes=["key"], to_classes=["value"])],
        )


def test_duplicate_class_names_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate class names"):
        LabelOntology(classes=[LabelClass(name="a"), LabelClass(name="a")])


def test_duplicate_attribute_names_within_a_class_are_refused() -> None:
    with pytest.raises(ValueError, match="duplicate attribute"):
        LabelClass(name="c", attributes=[OutputAttr(name="x"), OutputAttr(name="x")])


def test_kind_is_a_free_vocabulary_not_a_closed_enum() -> None:
    """A new task type must not require editing Python and a redeploy. HF's own taxonomy is a
    vocabulary for routing and filtering, not a contract — only 31 of its 57 ids carry any schema."""
    for kind in ("object-detection", "document-question-answering", "riksarkivet-landskapshandlingar-v2"):
        assert LabelOntology(kind=kind).kind == kind


# ── carried over from test_task_templates.py, whose subject this model replaced ────────────────────
#
# `TaskTemplate` is gone, but three of its properties are properties of the REPLACEMENT and would
# have been silently lost with the file. They are re-pinned here against the ontology.


def test_a_REQUIRED_attribute_that_is_absent_is_refused() -> None:
    """`required` is the half of attribute typing the optional-path test cannot reach: an absent
    optional field is fine, an absent required one is the whole point of declaring it."""
    ontology = LabelOntology(classes=[LabelClass(name="c", attributes=[OutputAttr(name="note", required=True)])])

    violation = validate_against_ontology(ontology, [_shape(label="c")])
    assert violation is not None and "note" in violation

    # An empty string is ABSENT, not a value — otherwise a required free-text field is satisfied by
    # having been touched, which is what "required" is supposed to prevent.
    assert validate_against_ontology(ontology, [_shape(label="c", note="")]) is not None
    assert validate_against_ontology(ontology, [_shape(label="c", note="seen")]) is None


def test_a_mistyped_key_is_REFUSED_not_dropped() -> None:
    """The adversarial audit's finding, re-pinned on the object that inherited it.

    Under pydantic's default an unknown key is dropped, so `enforced` (for `enforce`) created a 201
    with enforcement silently off — a contract advertised and never applied. `enforce` no longer
    exists, but every enforcement-bearing field here still defaults PERMISSIVE, so the same typo
    class still costs a constraint. `extra="forbid"` is what makes it an error instead of a shrug,
    and it is only load-bearing while something checks it.
    """
    with pytest.raises(ValueError, match="requird"):
        LabelClass.model_validate({"name": "stamp", "requird": True})
    with pytest.raises(ValueError, match="require"):
        OutputAttr.model_validate({"name": "order", "type": "int", "require": True})
    with pytest.raises(ValueError, match="klasses"):
        LabelOntology.model_validate({"kind": "object-detection", "klasses": []})
    with pytest.raises(ValueError, match="directd"):
        RelationClass.model_validate({"name": "answers", "directd": False})
