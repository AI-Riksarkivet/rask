"""The RECIPE family (open-bulk phase 3): an LLM/VLM answering an item-level question.

The bulk grid's "＋ column" fills cells through the ordinary assist POST — the producer's
answer is a `tag` shape whose `text` IS the cell value. These tests pin the three facts the
grid depends on: the wire carries a textual answer at all (`AssistShape.text`), the `vlm`
family is registered interactive with a `tag` return (so a tag-tooled recipe column computes
compatible), and the mock answers deterministically so the whole fill → correct → validate
loop is exercisable in-repo.
"""

from __future__ import annotations

from annotator.api.v1.endpoints.assist import AssistRequest, _mock, _within_contract
from annotator.projects.ontology import LabelClass, LabelOntology


def test_a_tag_tooled_task_keeps_the_recipe_answer() -> None:
    """The contract filter admits the answer when the ontology declares a tag-tooled class —
    the class the act-first add-column derives."""
    ontology = LabelOntology(classes=[LabelClass(name="century", tools=["tag"], transcribe=True)])
    shapes = _mock(AssistRequest(producer="vlm", prompt="century?"))
    kept, dropped = _within_contract(shapes, ontology)
    assert [s.shape_type for s in kept] == ["tag"] and dropped == []
