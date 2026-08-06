"""Inter-annotator agreement that knows what a shape IS (#55).

The scorer this replaces compared LABEL MULTISETS: a replica group agreed when every member's
sorted list of label strings was identical. Three things were wrong with it, and all three make a
number that reads like quality but is not:

1. **Geometry was discarded entirely.** Two annotators drawing boxes in completely different places
   with the same label scored as PERFECT agreement. For a detection task that is the whole question.
2. **No chance correction.** With a two-class taxonomy, two annotators labelling at random agree
   half the time; an uncorrected percentage reports that as 50% and a reader takes it for signal.
3. **An all-empty group scored perfect.** `[] == []`, so a group where every replica was skipped
   counted as agreement — the one case where there is no evidence at all.

The ruling (owner's call, 2026-08-06): match shapes at **IoU >= 0.5** — the COCO/PASCAL convention,
FIXED rather than per-project so two projects' numbers mean the same thing — and report **Fleiss'
kappa**, which is defined for N raters. Cohen's is strictly pairwise; averaging it over three
replicas is not a defined statistic, and someone would inevitably compare that average to a
published Fleiss number.

Everything here is pure. The publish facet is assembled in `publish.py`; this module never reads a
project, a plan or a table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shapely.geometry import Polygon


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


#: Two shapes are the same OBJECT at or above this overlap. Fixed on purpose — see the module note.
IOU_MATCH_THRESHOLD = 0.5

#: The label a rater assigns when they did not mark this object at all. MISSING an object is
#: disagreement, not absence of data: without this, a group where one annotator found three regions
#: and another found none would be scored on the three the first one found and look unanimous.
NOT_ANNOTATED = "\x00none"

#: Shape kinds carrying no geometry to compare. They are matched on LABEL alone, which is correct
#: rather than a fallback: a whole-item tag has no extent, so two taggers agree exactly when the tag
#: agrees.
_LABEL_ONLY_KINDS = frozenset({"tag", "text", "none"})


def _bbox_iou(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    """Axis-aligned rectangle IoU."""
    ax, ay = float(a.get("x") or 0.0), float(a.get("y") or 0.0)
    aw, ah = float(a.get("width") or 0.0), float(a.get("height") or 0.0)
    bx, by = float(b.get("x") or 0.0), float(b.get("y") or 0.0)
    bw, bh = float(b.get("width") or 0.0), float(b.get("height") or 0.0)
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _polygon_iou(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    """True AREA IoU for polygons, via shapely.

    The owner chose this over the polygon's bounding box: an extent-based score calls two very
    different shapes sharing a bounding box "agreed", which for page-region work (a curved margin
    note against a full column) is exactly the case that matters.

    `buffer(0)` repairs self-intersecting rings rather than raising. A human-drawn polygon that
    crosses itself is a drawing artefact, not a reason to fail a publish facet — and shapely's
    predicates are undefined on invalid geometry, so the alternative is a wrong number, not an
    error.
    """
    pa, pb = _as_polygon(a.get("polygon")), _as_polygon(b.get("polygon"))
    if pa is None or pb is None:
        return 0.0
    inter = pa.intersection(pb).area
    union = pa.union(pb).area
    return inter / union if union > 0 else 0.0


def _as_polygon(flat: Sequence[float] | None) -> Polygon | None:
    """`[x0, y0, x1, y1, ...]` → a valid shapely polygon, or None when it cannot be one."""
    if not flat or len(flat) < 6 or len(flat) % 2 != 0:  # < 3 points is not an area
        return None
    poly = Polygon([(float(flat[i]), float(flat[i + 1])) for i in range(0, len(flat), 2)])
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if poly.is_valid and poly.area > 0 else None


def _segment_iou(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    """1-D overlap for temporal segments — the audio/video analogue of a box."""
    a0, a1 = float(a.get("t_start") or 0.0), float(a.get("t_end") or 0.0)
    b0, b1 = float(b.get("t_start") or 0.0), float(b.get("t_end") or 0.0)
    if a1 <= a0 or b1 <= b0:
        return 0.0
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = (a1 - a0) + (b1 - b0) - inter
    return inter / union if union > 0 else 0.0


def shape_iou(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    """Overlap of two shapes, dispatched on kind. Different kinds never match: a box and a segment
    describe different things, and scoring them against each other would invent a number."""
    kind = str(a.get("shape_type") or "")
    if kind != str(b.get("shape_type") or ""):
        return 0.0
    if kind in _LABEL_ONLY_KINDS:
        # No extent to compare. Treated as fully overlapping so MATCHING falls through to the label,
        # which is the whole of agreement for a tag.
        return 1.0
    if kind == "polygon" or kind == "mask":
        return _polygon_iou(a, b)
    if kind == "segment":
        return _segment_iou(a, b)
    return _bbox_iou(a, b)


def match_shapes(
    reference: Sequence[Mapping[str, Any]],
    other: Sequence[Mapping[str, Any]],
    *,
    threshold: float = IOU_MATCH_THRESHOLD,
) -> dict[int, int]:
    """Greedy best-first matching of `other` onto `reference`, by IoU. Returns reference-index →
    other-index for pairs at or above `threshold`.

    Best-first rather than first-fit: taking pairs in descending IoU means a shape cannot be
    consumed by a poor early match and leave its true partner unmatched. Each index is used once,
    so this is a matching and not a lookup — an annotator drawing two overlapping boxes cannot have
    both claim the same reference shape.

    Deliberately NOT label-gated: two annotators who agree an object is THERE but disagree what it
    IS have made exactly one disagreement, and that is the disagreement kappa is for. Gating here
    would instead record it as two — a miss and a spurious extra — and double-count it.
    """
    scored: list[tuple[float, int, int, int]] = []
    for i, r in enumerate(reference):
        for j, o in enumerate(other):
            iou = shape_iou(r, o)
            if iou >= threshold:
                # Label as a TIE-BREAK only, never as a gate. Two annotators who both drew the same
                # two boxes over one object — a `person` and a `ship` at identical coordinates —
                # are ambiguous to geometry alone, and pairing them arbitrarily invents a
                # disagreement out of an ordering accident. Among equally-good geometric candidates
                # the matching label is the right pair; it still cannot CREATE a match below the
                # threshold, so a genuine label disagreement on one object stays one disagreement.
                mismatch = 0 if str(r.get("label") or "") == str(o.get("label") or "") else 1
                scored.append((iou, mismatch, i, j))
    scored.sort(key=lambda t: (-t[0], t[1], t[2], t[3]))
    used_ref: set[int] = set()
    used_other: set[int] = set()
    pairs: dict[int, int] = {}
    for _, _, i, j in scored:
        if i in used_ref or j in used_other:
            continue
        used_ref.add(i)
        used_other.add(j)
        pairs[i] = j
    return pairs


def rating_table(per_rater: Sequence[Sequence[Mapping[str, Any]]]) -> list[list[str]]:
    """Cluster every rater's shapes into OBJECT SLOTS, and return one label per rater per slot.

    A slot is one thing in the item. Each rater contributes the label they gave it, or
    `NOT_ANNOTATED` if they did not mark it — which is what makes a missed object count as
    disagreement rather than vanish.

    Slots are seeded from the first rater and grown: a shape matching no existing slot opens a new
    one. That makes the result order-dependent in principle; in practice a shape matches at most one
    slot at IoU >= 0.5, because two slots that both matched it would have to overlap each other by
    more than half and would already be one slot.
    """
    if not per_rater:
        return []
    n_raters = len(per_rater)
    slots: list[list[Mapping[str, Any] | None]] = []
    for rater, shapes in enumerate(per_rater):
        taken: set[int] = set()
        # Existing slots first: match this rater's shapes against each slot's seed.
        for slot in slots:
            seed = next((s for s in slot if s is not None), None)
            if seed is None:
                continue
            seed_label = str(seed.get("label") or "")
            # (-iou, label-mismatch) — geometry first, then the label as a tie-break, exactly as
            # in `match_shapes`. Identical shapes bearing different labels are otherwise paired by
            # enumeration order, which manufactures a disagreement out of an accident.
            best: tuple[float, int, int] | None = None
            for idx, shape in enumerate(shapes):
                if idx in taken:
                    continue
                iou = shape_iou(seed, shape)
                if iou < IOU_MATCH_THRESHOLD:
                    continue
                mismatch = 0 if str(shape.get("label") or "") == seed_label else 1
                candidate = (-iou, mismatch, idx)
                if best is None or candidate < best:
                    best = candidate
            best_idx = best[2] if best is not None else -1
            if best_idx >= 0:
                taken.add(best_idx)
                slot[rater] = shapes[best_idx]
        # Anything left is an object no existing slot describes.
        for idx, shape in enumerate(shapes):
            if idx in taken:
                continue
            fresh: list[Mapping[str, Any] | None] = [None] * n_raters
            fresh[rater] = shape
            slots.append(fresh)
    return [[str(s.get("label") or "") if s is not None else NOT_ANNOTATED for s in slot] for slot in slots]


def fleiss_kappa(table: Sequence[Sequence[str]]) -> float | None:
    """Fleiss' kappa over `table[slot][rater] = category`.

    Returns None when the statistic is UNDEFINED rather than a number that looks like agreement:
    with fewer than two raters there is nothing to agree about, with no slots there is no evidence,
    and when every rater picked the same category for every slot the expected agreement is 1 and
    kappa is 0/0. That last case is the one worth naming — it is unanimity, and reporting it as
    kappa 0 (the value the division would suggest) would invert the reading completely.
    """
    slots = [row for row in table if row]
    if not slots:
        return None
    n = len(slots[0])
    if n < 2 or any(len(row) != n for row in slots):
        return None
    categories = sorted({c for row in slots for c in row})
    if len(categories) < 2:
        return None  # unanimous across the board: agreement is total, kappa is undefined (0/0)

    p_i: list[float] = []
    counts_total: dict[str, int] = dict.fromkeys(categories, 0)
    for row in slots:
        counts = dict.fromkeys(categories, 0)
        for c in row:
            counts[c] += 1
            counts_total[c] += 1
        p_i.append((sum(v * v for v in counts.values()) - n) / (n * (n - 1)))
    p_bar = sum(p_i) / len(slots)
    total = len(slots) * n
    p_e = sum((v / total) ** 2 for v in counts_total.values())
    if p_e >= 1.0:
        return None
    return (p_bar - p_e) / (1 - p_e)


def group_scores(per_rater: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Agreement for ONE replica group: the slots, whether it was unanimous, and its kappa."""
    table = rating_table(per_rater)
    unanimous = bool(table) and all(len(set(row)) == 1 for row in table)
    return {
        "objects": len(table),
        "unanimous": unanimous,
        "kappa": fleiss_kappa(table),
    }


def summarize(groups: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Roll per-group scores into the publish facet.

    `mean_kappa` skips groups whose kappa is undefined rather than substituting 0 — a unanimous
    group would otherwise drag the average DOWN, which is precisely backwards.
    """
    scored = list(groups)
    kappas = [g["kappa"] for g in scored if g.get("kappa") is not None]
    return {
        "groups": len(scored),
        "unanimous_groups": sum(1 for g in scored if g.get("unanimous")),
        "objects": sum(int(g.get("objects") or 0) for g in scored),
        "mean_kappa": (sum(kappas) / len(kappas)) if kappas else None,
        "kappa_defined_for": len(kappas),
        "iou_threshold": IOU_MATCH_THRESHOLD,
    }
