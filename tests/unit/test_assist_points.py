"""Multi-point prompting — the SAM-style interactive refinement session, on the wire.

A SAM-family backend takes points (foreground/background clicks), boxes, or both; the
request carries the FULL point set each time so the backend stays stateless and one more
click REFINES the same object. These tests pin the wire shape (points parse and reach a
remote backend verbatim) and the mock's session semantics: the mask follows the POSITIVE
points, and every added point visibly improves the scores — without that, the client's
replace-on-refine loop would swap a prediction for an identical one and the whole session
would be indistinguishable from a no-op.
"""

from __future__ import annotations

from typing import Any

from annotator.api.v1.endpoints.assist import _SAM_CLICK_PATCH, AssistRequest, Point, Region, _mock, _remote

from service_kit.media.state import AppState


def test_points_parse_on_the_request_and_default_empty() -> None:
    """The wire shape: `points` is optional (every existing caller sends none) and signed
    (positive defaults to the foreground click, the common case)."""
    bare = AssistRequest(producer="sam-click")
    assert bare.points == []

    req = AssistRequest.model_validate({"producer": "sam-click", "points": [{"x": 10, "y": 20}, {"x": 30, "y": 40, "positive": False}]})
    assert [p.positive for p in req.points] == [True, False]


def test_the_mask_follows_the_POSITIVE_points() -> None:
    """Two foreground clicks ⇒ the patch spans their bounding box (padded); a background
    click steers a real model but must not DRAG the selection toward what it excludes."""
    (shape,) = _mock(
        AssistRequest(
            producer="sam-click",
            points=[Point(x=100, y=100), Point(x=300, y=200), Point(x=900, y=900, positive=False)],
        )
    )
    pad = _SAM_CLICK_PATCH / 2
    assert shape.shape_type == "polygon"
    assert (shape.x, shape.y) == (100 - pad, 100 - pad)
    assert (shape.width, shape.height) == (200 + 2 * pad, 100 + 2 * pad)
    # The excluded point stays outside the patch entirely.
    assert shape.x + shape.width < 900


def test_each_added_point_visibly_improves_the_scores() -> None:
    """The refinement loop's observable: same object, better answer. Monotone confidence up,
    uncertainty down — so the client's replace-on-refine visibly changes the row it replaces."""
    one = _mock(AssistRequest(producer="sam-click", points=[Point(x=50, y=50)]))[0]
    two = _mock(AssistRequest(producer="sam-click", points=[Point(x=50, y=50), Point(x=60, y=60)]))[0]
    three = _mock(AssistRequest(producer="sam-click", points=[Point(x=50, y=50), Point(x=60, y=60), Point(x=70, y=40, positive=False)]))[0]

    assert one.confidence < two.confidence < three.confidence
    assert one.uncertainty is not None and two.uncertainty is not None and three.uncertainty is not None
    assert one.uncertainty > two.uncertainty > three.uncertainty


def test_a_pure_background_session_still_answers_somewhere() -> None:
    """All-negative clicks anchor the patch anyway — a session must never return nothing
    because its author started by excluding."""
    (shape,) = _mock(AssistRequest(producer="sam-click", points=[Point(x=400, y=400, positive=False)]))
    assert shape.width > 0 and shape.height > 0


def test_a_point_session_beats_the_region_default_not_the_region_itself() -> None:
    """No points ⇒ the drawn region wins, exactly as before — the session is additive, and
    every pre-points caller keeps its behaviour bit-for-bit."""
    (shape,) = _mock(AssistRequest(producer="sam-click", region=Region(x=10, y=10, width=50, height=50)))
    assert (shape.x, shape.y, shape.width, shape.height) == (10, 10, 50, 50)
    assert shape.confidence == 0.85 and shape.uncertainty == 0.3


def test_remote_backends_receive_the_full_point_set() -> None:
    """A real model server gets the whole signed session, not a summary — the backend is the
    party that knows what to do with background clicks, so nothing may be filtered en route."""
    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict[str, Any]:
            return {"shapes": []}

    class _Http:
        def post(self, url: str, json: dict[str, Any], timeout: float) -> _Resp:
            captured.update(json)
            return _Resp()

    body = AssistRequest(producer="sam-click", points=[Point(x=1, y=2), Point(x=3, y=4, positive=False)])
    _remote(AppState(http=_Http()), "http://model", ("d1", 0, 0), body)

    assert captured["points"] == [
        {"x": 1.0, "y": 2.0, "positive": True},
        {"x": 3.0, "y": 4.0, "positive": False},
    ]
