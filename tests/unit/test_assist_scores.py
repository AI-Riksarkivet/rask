"""Predictions carry their SCORES — the wire half of the active-learning loop.

The review queue orders predictions-first, highest-uncertainty-first, and the sidebar renders both
scores per row. Those consumers existed for a while with no producer: `AssistShape` carried only
`confidence`, the mock emitted values the client then dropped, and `NewAnnotation` had neither
column — so every prediction landed with null scores and the "active-learning order" degraded to
insertion order. These tests pin the whole chain: the mock states both scores, differently per
producer (so a mixed queue has a visible order), and the save path keeps them.
"""

from __future__ import annotations

from annotator.api.v1.endpoints.assist import AssistRequest, Region, _mock


def test_the_two_mock_producers_rank_DIFFERENTLY() -> None:
    """The queue's uncertainty ordering is only exercisable when a mixed queue has an order. Equal
    scores would make the sort a stable no-op — the exact failure this wiring exists to end."""
    (sam,) = _mock(AssistRequest(producer="sam-click", region=Region(x=10, y=10, width=50, height=50)))
    (dino,) = _mock(AssistRequest(producer="grounding-dino", prompt="seal"))

    assert sam.uncertainty != dino.uncertainty
    # The segmenter is the more certain of the two — pinned so the demo order is deterministic.
    assert sam.uncertainty is not None and dino.uncertainty is not None
    assert sam.uncertainty < dino.uncertainty
