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
    IOU_MATCH_THRESHOLD,
    NOT_ANNOTATED,
    fleiss_kappa,
    group_scores,
    match_shapes,
    rating_table,
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


def test_identical_boxes_overlap_completely() -> None:
    assert shape_iou(box(0, 0, 10, 10), box(0, 0, 10, 10)) == pytest.approx(1.0)


def test_disjoint_boxes_do_not_overlap_at_all() -> None:
    """THE case the old scorer got wrong: same label, nowhere near each other. It called this
    perfect agreement."""
    assert shape_iou(box(0, 0, 10, 10), box(100, 100, 10, 10)) == 0.0


def test_half_overlapping_boxes_score_a_third() -> None:
    """Worth stating numerically: two 10x10 boxes offset by 5 on one axis share 50 of 150 united
    area, so IoU is 1/3 — BELOW the 0.5 threshold. "Half overlapping" is not "the same object"."""
    assert shape_iou(box(0, 0, 10, 10), box(5, 0, 10, 10)) == pytest.approx(1 / 3)


def test_a_zero_area_box_matches_nothing() -> None:
    """A degenerate shape (a click, an aborted drag) must not divide by zero or match everything."""
    assert shape_iou(box(0, 0, 0, 0), box(0, 0, 10, 10)) == 0.0


def test_shapes_of_DIFFERENT_kinds_never_match() -> None:
    """A box and a temporal segment describe different things; scoring one against the other would
    invent a number out of unrelated coordinates."""
    assert shape_iou(box(0, 0, 10, 10), seg(0, 10)) == 0.0


def test_polygons_use_TRUE_AREA_not_the_bounding_box() -> None:
    """The owner chose shapely over an extent approximation for exactly this shape: two triangles
    splitting the same square share a bounding box entirely, but almost no area."""
    lower = poly([0, 0, 10, 0, 0, 10])
    upper = poly([10, 10, 10, 0, 0, 10])

    assert shape_iou(lower, upper) < 0.1


def test_a_SELF_INTERSECTING_polygon_is_repaired_not_raised() -> None:
    """A human-drawn polygon that crosses itself is a drawing artefact. shapely's predicates are
    undefined on invalid geometry, so the alternative to repairing is a wrong number, not an
    error."""
    bowtie = poly([0, 0, 10, 10, 10, 0, 0, 10])

    assert shape_iou(bowtie, bowtie) >= 0.0  # must not raise


def test_a_polygon_with_too_few_points_is_not_an_area() -> None:
    assert shape_iou(poly([0, 0, 1, 1]), poly([0, 0, 1, 1])) == 0.0


def test_temporal_segments_overlap_in_ONE_dimension() -> None:
    """Audio/video's analogue of a box: [0,10] and [5,15] share 5 of 15 united seconds."""
    assert shape_iou(seg(0, 10), seg(5, 15)) == pytest.approx(1 / 3)


def test_a_TAG_has_no_extent_so_it_falls_through_to_the_label() -> None:
    """Label-only kinds are matched on their label, which is correct rather than a fallback: a
    whole-item tag has no geometry, so agreement IS label agreement."""
    assert shape_iou({"shape_type": "tag", "label": "letter"}, {"shape_type": "tag", "label": "x"}) == 1.0


# ── matching ────────────────────────────────────────────────────────────────────────────────────


def test_matching_is_BEST_FIRST_not_first_fit() -> None:
    """A shape must not be consumed by a poor early match, leaving its true partner unmatched.
    Reference[0] overlaps both candidates, but candidate 1 is the better pair for it."""
    reference = [box(0, 0, 10, 10), box(0, 0, 10, 10)]
    other = [box(3, 0, 10, 10), box(0, 0, 10, 10)]

    pairs = match_shapes(reference, other)

    assert pairs[0] == 1


def test_each_shape_is_used_at_most_ONCE() -> None:
    """An annotator drawing two overlapping boxes cannot have both claim the same reference shape —
    that is what makes this a matching rather than a lookup."""
    pairs = match_shapes([box(0, 0, 10, 10)], [box(0, 0, 10, 10), box(1, 1, 10, 10)])

    assert len(pairs) == 1


def test_below_the_threshold_is_NOT_a_match() -> None:
    assert match_shapes([box(0, 0, 10, 10)], [box(5, 0, 10, 10)]) == {}


def test_matching_ignores_the_LABEL_on_purpose() -> None:
    """Two annotators who agree an object is THERE but disagree what it IS have made exactly ONE
    disagreement, and that is what kappa is for. Gating the match on label would record it as two —
    a miss and a spurious extra — and double-count it."""
    pairs = match_shapes([box(0, 0, 10, 10, "person")], [box(0, 0, 10, 10, "ship")])

    assert pairs == {0: 0}


# ── slots ───────────────────────────────────────────────────────────────────────────────────────


def test_a_MISSED_object_is_recorded_as_disagreement_not_absence() -> None:
    """The failure the sentinel exists for. Without it, a group where one annotator found an object
    and the other found none would be scored only on what they both saw, and look unanimous."""
    table = rating_table([[box(0, 0, 10, 10)], []])

    assert table == [["person", NOT_ANNOTATED]]


def test_the_same_object_seen_by_both_is_ONE_slot() -> None:
    table = rating_table([[box(0, 0, 10, 10)], [box(1, 1, 10, 10)]])

    assert len(table) == 1


def test_DIFFERENT_objects_are_different_slots() -> None:
    table = rating_table([[box(0, 0, 10, 10)], [box(100, 100, 10, 10)]])

    assert len(table) == 2
    assert all(NOT_ANNOTATED in row for row in table)


def test_the_same_object_labelled_differently_is_one_slot_two_labels() -> None:
    """Geometric agreement, semantic disagreement — the case the old label-multiset scorer could
    not express at all."""
    table = rating_table([[box(0, 0, 10, 10, "person")], [box(0, 0, 10, 10, "ship")]])

    assert table == [["person", "ship"]]


# ── kappa ───────────────────────────────────────────────────────────────────────────────────────


def test_kappa_is_UNDEFINED_when_everyone_agrees_on_everything() -> None:
    """Unanimity makes expected agreement 1 and kappa 0/0. Returning the 0 the division suggests
    would invert the reading completely — total agreement reported as none."""
    assert fleiss_kappa([["a", "a"], ["a", "a"]]) is None


def test_kappa_is_UNDEFINED_with_no_slots() -> None:
    """No evidence is not agreement. The old scorer counted an all-empty replica group as PERFECT."""
    assert fleiss_kappa([]) is None


def test_kappa_is_UNDEFINED_with_fewer_than_two_raters() -> None:
    assert fleiss_kappa([["a"], ["b"]]) is None


def test_total_disagreement_is_NEGATIVE() -> None:
    """Worse than chance. A percentage cannot express this, which is the whole point of correcting
    for chance."""
    kappa = fleiss_kappa([["a", "b"], ["a", "b"], ["a", "b"]])

    assert kappa is not None
    assert kappa < 0


def test_partial_agreement_lands_between_the_extremes() -> None:
    kappa = fleiss_kappa([["a", "a"], ["a", "a"], ["a", "b"], ["b", "b"]])

    assert kappa is not None
    assert -1.0 <= kappa <= 1.0


def test_kappa_handles_THREE_raters_which_is_why_it_is_Fleiss() -> None:
    """Cohen's is strictly pairwise; with consensus_n=3 it would have to be averaged over three
    pairs, which is not a defined statistic. Fleiss takes N directly."""
    kappa = fleiss_kappa([["a", "a", "b"], ["b", "b", "b"], ["a", "a", "a"]])

    assert kappa is not None


# ── the facet ───────────────────────────────────────────────────────────────────────────────────


def test_a_group_where_everyone_drew_the_same_thing_is_unanimous() -> None:
    scores = group_scores([[box(0, 0, 10, 10)], [box(1, 1, 10, 10)]])

    assert scores["unanimous"] is True
    assert scores["objects"] == 1


def test_an_ALL_EMPTY_group_is_NOT_unanimous() -> None:
    """The old scorer's worst case: every replica skipped, `[] == []`, scored as perfect agreement.
    There is no evidence here, and 'unanimous' must not be how that reads."""
    scores = group_scores([[], []])

    assert scores["unanimous"] is False
    assert scores["objects"] == 0


def test_same_labels_in_DIFFERENT_PLACES_is_not_agreement() -> None:
    """The headline regression. The old scorer called this perfect."""
    scores = group_scores([[box(0, 0, 10, 10)], [box(100, 100, 10, 10)]])

    assert scores["unanimous"] is False


def test_the_summary_states_the_threshold_it_used() -> None:
    """A bare agreement number is unreadable without the threshold behind it — and someone will
    compare it to a published figure."""
    assert summarize([])["iou_threshold"] == IOU_MATCH_THRESHOLD


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


def test_identical_shapes_with_DIFFERENT_labels_pair_by_label_not_by_order() -> None:
    """Two annotators who both drew a `person` box and a `ship` box over the same spot AGREE. With
    geometry alone the pairing is ambiguous — every candidate scores IoU 1.0 — so enumeration order
    decides, and reversing the second annotator's order would manufacture a disagreement out of an
    accident. The label breaks the tie; it still cannot create a match below the threshold, so a
    genuine label disagreement on ONE object remains one disagreement (see the test above).
    """
    a = [box(1, 1, 2, 2, "person"), box(1, 1, 2, 2, "ship")]
    b = [box(1, 1, 2, 2, "ship"), box(1, 1, 2, 2, "person")]

    assert group_scores([a, b])["unanimous"] is True
