"""The RECIPE family (open-bulk phase 3): an LLM/VLM answering an item-level question.

The bulk grid's "＋ column" fills cells through the ordinary assist POST — the producer's
answer is a `tag` shape whose `text` IS the cell value. These tests pin the three facts the
grid depends on: the wire carries a textual answer at all (`AssistShape.text`), the `vlm`
family is registered interactive with a `tag` return (so a tag-tooled recipe column computes
compatible), and the mock answers deterministically so the whole fill → correct → validate
loop is exercisable in-repo.
"""

from __future__ import annotations

from annotator.api.v1.endpoints.assist import _BATCH_ONLY, _RETURNS, AssistRequest, AssistShape, _mock, _within_contract
from annotator.projects.ontology import LabelClass, LabelOntology


def test_the_wire_carries_a_textual_answer() -> None:
    """`text` is part of the shape contract with an empty default — geometry producers are
    untouched, text producers finally have a channel."""
    assert AssistShape(x=0, y=0, width=0, height=0).text == ""
    shape = AssistShape.model_validate({"x": 0, "y": 0, "width": 0, "height": 0, "text": "17th"})
    assert shape.text == "17th"


def test_vlm_is_an_interactive_tag_family() -> None:
    """Interactive (per-cell fills ride the assist POST, unlike batch-only htr) and returning
    `tag` — the shape an item-level answer lands as."""
    assert _RETURNS["vlm"] == ("tag",)
    assert "vlm" not in _BATCH_ONLY


def test_the_mock_answers_the_question_as_a_tag_with_text() -> None:
    """Deterministic echo, honest about being a mock — and shaped exactly as the grid consumes
    it: one `tag` shape, the answer in `text`, scores stated."""
    (shape,) = _mock(AssistRequest(producer="vlm", prompt="which century is this from?"))
    assert shape.shape_type == "tag"
    assert shape.text == "[vlm] which century is this from?"
    assert shape.confidence > 0 and shape.uncertainty is not None


def test_a_tag_tooled_task_keeps_the_recipe_answer() -> None:
    """The contract filter admits the answer when the ontology declares a tag-tooled class —
    the class the act-first add-column derives."""
    ontology = LabelOntology(classes=[LabelClass(name="century", tools=["tag"], transcribe=True)])
    shapes = _mock(AssistRequest(producer="vlm", prompt="century?"))
    kept, dropped = _within_contract(shapes, ontology)
    assert [s.shape_type for s in kept] == ["tag"] and dropped == []
