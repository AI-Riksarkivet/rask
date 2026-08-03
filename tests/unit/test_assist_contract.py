"""A model's output is normalized and checked against the TASK — at the boundary, not by a human.

Two holes met here, and the owner named the second one: "schemas must align from the ml backend".

1. `AssistShape.shape_type` defaulted to `"rectangle"` and was a free string. That name is accepted
   by neither the service vocabulary (`bbox`) nor the canvas, so a prediction arrived speaking a
   dialect nothing downstream understood — the same engine/service split that stopped five of seven
   drawing tools from submitting.
2. NOTHING checked a prediction against the task's own contract. A producer returning a polygon for
   a bbox-only task put that shape in the review queue, and it was refused at SUBMIT — after a human
   had already looked at it. The mismatch was real; it was just deferred onto the annotator.

A producer is not obliged to know the task's rules; the registry exists so a backend is a config
entry. So both are resolved once, server-side, where the task's captured template lives.
"""

from __future__ import annotations

import pytest
from annotator.api.v1.endpoints.assist import _CANONICAL_SHAPE, AssistShape, _within_contract


def _shape(shape_type: str) -> AssistShape:
    return AssistShape(shape_type=shape_type, x=0, y=0, width=10, height=10)


@pytest.mark.parametrize(
    ("produced", "canonical"),
    [
        ("rectangle", "bbox"),  # this endpoint's OWN old default — accepted by neither side
        ("rect", "bbox"),
        ("box", "bbox"),
        ("point", "keypoint"),
        ("line", "polyline"),
        ("baseline", "polyline"),
        ("polygon", "polygon"),
        ("mask", "mask"),
    ],
)
def test_a_producers_dialect_is_normalized_to_the_canonical_vocabulary(produced: str, canonical: str) -> None:
    assert _CANONICAL_SHAPE[produced] == canonical


@pytest.mark.asyncio
async def test_an_assist_with_no_task_is_unconstrained() -> None:
    """The ad-hoc canvas has no contract, so there is nothing to check against."""
    shapes = [_shape("polygon"), _shape("bbox")]
    kept, dropped = await _within_contract(shapes, None)
    assert len(kept) == 2
    assert dropped == []


@pytest.mark.asyncio
async def test_a_prediction_the_task_refuses_is_dropped_and_REPORTED(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropped, not silently: the operator has to learn the backend disagrees with the task.

    Silently filtering would leave a producer permanently returning work nobody sees, with nothing
    anywhere saying so — the failure mode where a model looks configured and does nothing.
    """
    _stub_task(monkeypatch, {"kind": "bbox-detection", "tools": ["bbox"], "enforce": True})

    kept, dropped = await _within_contract([_shape("bbox"), _shape("polygon")], "t1")

    assert [s.shape_type for s in kept] == ["bbox"]
    assert len(dropped) == 1
    assert "polygon" in dropped[0] and "bbox" in dropped[0]


@pytest.mark.asyncio
async def test_an_unenforced_template_filters_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unenforced template is a suggestion. Dropping predictions on one would discard work the
    server would happily have accepted at submit."""
    _stub_task(monkeypatch, {"kind": "bbox-detection", "tools": ["bbox"], "enforce": False})

    kept, dropped = await _within_contract([_shape("polygon")], "t1")

    assert len(kept) == 1
    assert dropped == []


@pytest.mark.asyncio
async def test_an_unreadable_task_returns_the_predictions_unfiltered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail OPEN, deliberately, and only here.

    Everywhere a write is gated this codebase fails closed. This is not a gate: submit still refuses
    a violating shape, so the contract holds regardless. Losing a real prediction because a rule
    could not be READ would be a worse failure than the mismatch this guards against — and it would
    be invisible, because the annotator would simply see fewer suggestions.
    """

    class _Boom:
        async def get(self) -> dict:
            raise RuntimeError("actor unreachable")

    import annotator.api.v1.endpoints.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "_proxy", lambda _tid: _Boom())

    kept, dropped = await _within_contract([_shape("polygon")], "t1")

    assert len(kept) == 1, "a prediction was lost because a rule could not be read"
    assert dropped == []


def _stub_task(monkeypatch: pytest.MonkeyPatch, template: dict) -> None:
    class _Task:
        async def get(self) -> dict:
            return {"template": template}

    import annotator.api.v1.endpoints.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "_proxy", lambda _tid: _Task())
