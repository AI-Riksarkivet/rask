"""Geometry-aware, chance-corrected inter-annotator agreement (#55).

The scorer this replaces compared label MULTISETS, which meant two annotators drawing boxes in
completely different places with the same labels scored as perfect agreement. These pin the three
things that fixes — geometry, chance correction, and the empty group — plus the cases where the
statistic is UNDEFINED and must say so rather than return a number that reads like quality.

Owner's ruling 2026-08-06: IoU >= 0.5 fixed (COCO/PASCAL convention), Fleiss' kappa (defined for N
raters, unlike Cohen's), true polygon area IoU via shapely.
"""

from __future__ import annotations

import pytest

from annotator.projects.agreement import (
    fleiss_kappa,
    group_scores,
    match_shapes,
    shape_iou,
    summarize,
)


def box(x: float, y: float, w: float, h: float, label: str = "person") -> dict[str, object]:
    return {"shape_type": "bbox", "x": x, "y": y, "width": w, "height": h, "label": label}


def poly(points: list[float], label: str = "region") -> dict[str, object]:
    return {"shape_type": "polygon", "polygon": points, "label": label}


def seg(t0: float, t1: float, label: str = "speech") -> dict[str, object]:
    return {"shape_type": "segment", "t_start": t0, "t_end": t1, "label": label}


# ── geometry ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b", "iou"),
    [
        # Two 10x10 boxes offset by 5 on one axis share 50 of 150 united area, so IoU is 1/3 — BELOW
        # the 0.5 threshold. "Half overlapping" is not "the same object".
        pytest.param(box(0, 0, 10, 10), box(5, 0, 10, 10), 1 / 3, id="half-overlapping-boxes"),
        # True area, not the bounding box: two triangles splitting one square share its whole bounding
        # box and none of its area. The owner chose shapely over an extent approximation for this shape.
        pytest.param(poly([0, 0, 10, 0, 0, 10]), poly([10, 10, 10, 0, 0, 10]), 0.0, id="triangles-splitting-a-square"),
        # A polygon that crosses itself is a drawing artefact. shapely's predicates are undefined on
        # invalid geometry, so it is repaired rather than raised, and the repaired shape matches itself.
        pytest.param(poly([0, 0, 10, 10, 10, 0, 0, 10]), poly([0, 0, 10, 10, 10, 0, 0, 10]), 1.0, id="self-intersecting-polygon"),
        pytest.param(poly([0, 0, 1, 1]), poly([0, 0, 1, 1]), 0.0, id="polygon-with-too-few-points"),
        # Audio/video's analogue of a box: [0,10] and [5,15] share 5 of 15 united seconds.
        pytest.param(seg(0, 10), seg(5, 15), 1 / 3, id="half-overlapping-segments"),
        # A box and a temporal segment describe different things; scoring one against the other would
        # invent a number out of unrelated coordinates.
        pytest.param(box(0, 0, 10, 10), seg(0, 10), 0.0, id="box-against-segment"),
        # A whole-item tag has no geometry, so agreement IS label agreement and the extent always matches.
        pytest.param({"shape_type": "tag", "label": "letter"}, {"shape_type": "tag", "label": "x"}, 1.0, id="tags-have-no-extent"),
    ],
)
def test_iou_is_the_true_shared_extent_of_two_shapes(a: dict[str, object], b: dict[str, object], iou: float) -> None:
    assert shape_iou(a, b) == pytest.approx(iou)


def test_a_zero_area_box_matches_nothing() -> None:
    """A degenerate shape (a click, an aborted drag) must not divide by zero or match everything."""
    assert shape_iou(box(0, 0, 0, 0), box(0, 0, 10, 10)) == 0.0


# ── matching ────────────────────────────────────────────────────────────────────────────────────


def test_matching_is_BEST_FIRST_not_first_fit() -> None:
    """A shape must not be consumed by a poor early match, leaving its true partner unmatched.
    Reference[0] overlaps both candidates, but candidate 1 is the better pair for it."""
    reference = [box(0, 0, 10, 10), box(0, 0, 10, 10)]
    other = [box(3, 0, 10, 10), box(0, 0, 10, 10)]

    pairs = match_shapes(reference, other)

    assert pairs[0] == 1


# ── kappa ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "table",
    [
        # Unanimity makes expected agreement 1 and kappa 0/0. Returning the 0 the division suggests
        # would invert the reading completely — total agreement reported as none.
        pytest.param([["a", "a"], ["a", "a"]], id="everyone-agrees-on-everything"),
        pytest.param([["a"], ["b"]], id="fewer-than-two-raters"),
    ],
)
def test_kappa_is_UNDEFINED_where_its_formula_has_no_answer(table: list[list[str]]) -> None:
    assert fleiss_kappa(table) is None


def test_kappa_is_UNDEFINED_with_no_slots() -> None:
    """No evidence is not agreement. The old scorer counted an all-empty replica group as PERFECT."""
    assert fleiss_kappa([]) is None


def test_total_disagreement_is_NEGATIVE() -> None:
    """Worse than chance. A percentage cannot express this, which is the whole point of correcting
    for chance."""
    kappa = fleiss_kappa([["a", "b"], ["a", "b"], ["a", "b"]])

    assert kappa is not None
    assert kappa < 0


# ── the facet ───────────────────────────────────────────────────────────────────────────────────


def test_an_ALL_EMPTY_group_is_NOT_unanimous() -> None:
    """The old scorer's worst case: every replica skipped, `[] == []`, scored as perfect agreement.
    There is no evidence here, and 'unanimous' must not be how that reads."""
    scores = group_scores([[], []])

    assert scores["unanimous"] is False
    assert scores["objects"] == 0


def test_mean_kappa_SKIPS_undefined_groups_rather_than_scoring_them_zero() -> None:
    """A unanimous group has undefined kappa. Substituting 0 would drag the average DOWN for the
    groups that agreed most — precisely backwards."""
    rolled = summarize([{"objects": 1, "unanimous": True, "kappa": None}, {"objects": 2, "unanimous": False, "kappa": 0.8}])

    assert rolled["mean_kappa"] == pytest.approx(0.8)
    assert rolled["kappa_defined_for"] == 1


def test_mean_kappa_is_None_when_NO_group_has_one() -> None:
    """Not 0.0 — that would read as "they disagreed" when the truth is "there is nothing to
    report"."""
    assert summarize([{"objects": 0, "unanimous": False, "kappa": None}])["mean_kappa"] is None
