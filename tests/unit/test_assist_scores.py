"""Predictions carry their SCORES — the wire half of the active-learning loop.

The review queue orders predictions-first, highest-uncertainty-first, and the sidebar renders both
scores per row. Those consumers existed for a while with no producer: `AssistShape` carried only
`confidence`, the mock emitted values the client then dropped, and `NewAnnotation` had neither
column — so every prediction landed with null scores and the "active-learning order" degraded to
insertion order. These tests pin the whole chain: the mock states both scores, differently per
producer (so a mixed queue has a visible order), and the save path keeps them.
"""

from __future__ import annotations

import pyarrow as pa
from annotator.annotations.save import new_rows
from annotator.annotations.schema import EMPTY_SCHEMA, NewAnnotation
from annotator.api.v1.endpoints.assist import AssistRequest, Region, _mock


def test_the_mock_states_BOTH_scores_for_every_producer() -> None:
    """A producer that omits its scores leaves predictions unrankable — the queue silently reads
    as alphabetical. The mock is the wire contract a real backend replaces, so it must state them."""
    for producer in ("sam", "grounding-dino"):
        (shape,) = _mock(AssistRequest(producer=producer, prompt="seal"))
        assert shape.confidence > 0.0, f"{producer}: no confidence"
        assert shape.uncertainty is not None and shape.uncertainty > 0.0, f"{producer}: no uncertainty"


def test_the_two_mock_producers_rank_DIFFERENTLY() -> None:
    """The queue's uncertainty ordering is only exercisable when a mixed queue has an order. Equal
    scores would make the sort a stable no-op — the exact failure this wiring exists to end."""
    (sam,) = _mock(AssistRequest(producer="sam-click", region=Region(x=10, y=10, width=50, height=50)))
    (dino,) = _mock(AssistRequest(producer="grounding-dino", prompt="seal"))

    assert sam.uncertainty != dino.uncertainty
    # The segmenter is the more certain of the two — pinned so the demo order is deterministic.
    assert sam.uncertainty is not None and dino.uncertainty is not None
    assert sam.uncertainty < dino.uncertainty


def test_uncertainty_is_NOT_derived_from_confidence() -> None:
    """`1 - confidence` carries no information beyond confidence (the ActiveLabelingSystem trap —
    its 'entropy' was exactly this, a monotone re-encoding). The mock's pairs must not obey it, so
    nobody can later 'simplify' the field into a derivation without a test going red."""
    for producer in ("sam", "grounding-dino"):
        (shape,) = _mock(AssistRequest(producer=producer, prompt="x"))
        assert shape.uncertainty != 1.0 - shape.confidence


def test_new_rows_keeps_the_scores_a_prediction_carries() -> None:
    """The save path: an inserted prediction's scores must land in the table, not fall to null."""
    ins = NewAnnotation(
        id="p1",
        shape_type="bbox",
        status="prediction",
        source="model:grounding-dino",
        confidence=0.7,
        uncertainty=0.45,
    )
    table = new_rows([ins], {"doc_id": "d1"}, _schema_with_identity())

    assert table.column("confidence").to_pylist() == [pa.scalar(0.7, pa.float32()).as_py()]
    assert table.column("uncertainty").to_pylist() == [pa.scalar(0.45, pa.float32()).as_py()]


def test_a_human_drawn_row_lands_with_NULL_scores_not_invented_ones() -> None:
    """A person's shape has no model score. A default of 1.0 would shuffle human rows into the
    model-ranked half of the queue; null sorts last, which is where reviewed-by-construction
    belongs."""
    ins = NewAnnotation(id="h1", shape_type="bbox")
    table = new_rows([ins], {"doc_id": "d1"}, _schema_with_identity())

    assert table.column("confidence").to_pylist() == [None]
    assert table.column("uncertainty").to_pylist() == [None]


def _schema_with_identity() -> pa.Schema:
    """EMPTY_SCHEMA with the one identity column these rows stamp — the same prepend-per-descriptor
    shape `scripts/seed_annotations.py` builds."""
    return pa.schema([pa.field("doc_id", pa.string()), *EMPTY_SCHEMA])
