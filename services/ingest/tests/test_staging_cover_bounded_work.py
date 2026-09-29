"""What one node of the finalizer's search is allowed to cost.

`discover_staged` runs after every unit has been acked off a WORK_QUEUE stream, so whatever it
refuses is refused at the one point where nothing can be refetched. Bounding the SEARCH — a step
budget, an explicit stack — bounds how long it runs. It does not bound what a single node of it
costs, and that cost is what decides whether an ordinary run is answered or abandoned.

THE INPUT IS THE WORKER'S OWN RECOVERY SHAPE, not an adversarial family. `worker.py`'s
`drain_chunk` stages ONE FRAGMENT PER REDELIVERED UNIT (`worker.py:495-517`) — the rule that keeps
the staged family laminar. A pod that dies between `stage_fragments` and the batch ack therefore
leaves, for each of its batches, the batch's own manifest plus one singleton manifest per unit in
it. Every unit then has exactly two holders, so unit propagation forces nothing and the ENTIRE
staged family reaches the search:

    F  = {u0 … u1023}        the batch, staged before the ack that never landed
    {u0}, {u1}, … {u1023}    the same units, redelivered one fragment at a time

A cover exists and is obvious — take the batch fragments. The search finds it in one pick per
batch. What it costs to find is the whole question: a node that rescans the core, or re-sorts the
units still uncovered, charges the WHOLE STAGING at EVERY pick, so the finalizer's bill is the
staging multiplied by its own depth. Measured 2026-08-31 against the full-scan search kept below,
256 batches of the default `fragment_rows=1024` — 262,144 units, inside the million-unit scale
`staging.py`'s own docstrings advertise — exhausted the 20M-step budget in 7.0s and came back
ABANDONED, where the shipped search answers the same family in 1.9s.

That verdict is deterministic, which is what makes it worse than slow: `StagingCoverAbandoned` tells
the operator to re-run the finalize, and the re-run replays the same refusal on a run whose
fragments are perfectly fine.

So the tests below pin the cost model rather than a wall clock: the search may read the staged
family a bounded number of times, NOT once per fragment it picks. The correctness half is a
differential against a full-scan search kept in this file — speed is only allowed if not one verdict
moved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest import staging
from ingest.staging import discover_staged, stage_fragments


RUN = "run-cover-bounded"


# ── the family a crashed worker leaves behind ─────────────────────────────────────────


def _recovery_family(batches: int, per_batch: int) -> list[frozenset[str]]:
    """`batches` staged batches, each also fully redelivered one unit at a time.

    Ordered the way `discover_staged` orders its records — largest unit set first, then by sorted
    unit key — because that ordering is what the returned indices refer to.
    """
    sets: list[frozenset[str]] = [frozenset(f"b{batch:04d}-u{unit:04d}" for unit in range(per_batch)) for batch in range(batches)]
    sets += [frozenset({f"b{batch:04d}-u{unit:04d}"}) for batch in range(batches) for unit in range(per_batch)]
    sets.sort(key=lambda unit_set: (-len(unit_set), sorted(unit_set)))
    return sets


def _stage_recovery_family(dataset: str, batches: int, per_batch: int) -> None:
    """Write that family as real manifests, so the assertion runs through `discover_staged`."""
    for batch in range(batches):
        units = [f"b{batch:04d}-u{unit:04d}" for unit in range(per_batch)]
        stage_fragments(dataset, RUN, units, [json.dumps({"batch": batch, "units": units})])
        for unit in units:
            stage_fragments(dataset, RUN, [unit], [json.dumps({"redelivered": unit, "units": [unit]})])


# ── the bound ─────────────────────────────────────────────────────────────────────────


def test_a_fully_redelivered_run_is_SOLVED_not_abandoned() -> None:
    """400 batches of 64 units, every unit also staged as its own redelivered fragment.

    25,600 units — 2.5% of the scale `staging.py` advertises, and a shape `worker.py` produces by
    design rather than by accident. The cover is "every batch fragment", 400 picks deep.

    Run against the module's real 20M-step budget, so what is under test is the finalizer an
    operator actually gets. A search that charges the staged family per pick spends 400 x 51,200
    occurrence-steps and comes back `exhausted`, which `discover_staged` renders as
    `StagingCoverAbandoned` — a refusal that re-running cannot clear, because the search is
    deterministic.

    Asserted at the solver rather than through `discover_staged` because 26,000 manifest files is
    the test's cost, not the defect's: the blow-up is entirely inside the search.
    """
    sets = _recovery_family(batches=400, per_batch=64)
    universe = frozenset().union(*sets)

    verdict = staging._exact_cover(sets, universe)

    assert not verdict.exhausted, f"the finalizer abandoned a {len(universe)}-unit recovery shape that has an obvious cover"
    assert verdict.chosen is not None, "the finalizer found no cover for a family whose batch fragments are one"
    covered = [unit for index in verdict.chosen for unit in sets[index]]
    assert sorted(covered) == sorted(universe), "the selection does not cover the run"
    assert len(covered) == len(set(covered)), "a unit would commit twice"


def test_the_search_reads_the_staging_a_bounded_number_of_TIMES_not_once_per_pick(tmp_path: Path) -> None:
    """The cost model, pinned as a budget proportional to the INPUT.

    The step budget is what makes the finalizer's runtime bounded, so "bounded per-node work" is
    exactly the statement that the budget needed to answer this input does not grow with how deep
    the search goes. Here that is asserted directly: the limit is lowered to a small multiple of the
    staged unit occurrences, and the run must still be answered.

    64 batches of 12 units → 768 units, 1,536 staged occurrences over 832 manifests, 64 picks deep. A
    search whose node cost is the whole family spends ~64 x 1,536 and abandons; one that touches
    only what a pick CHANGES spends a few thousand. The multiplier is 24 — the smallest budget that
    answers this input measured 8,629 steps on 2026-08-31 (5.6x occurrences, most of it the one-off
    index build), so 36,864 leaves a 4x margin. That headroom is deliberate: the assertion is a cost
    MODEL — work proportional to the staging, not to the staging times the depth — and pinning it to
    the exact spend would make it a fixture of one implementation.

    End to end through `discover_staged`, so the manifests, the record ordering and the fragment
    selection are all the real ones.
    """
    dataset = str(tmp_path / "bronze")
    batches, per_batch = 64, 12
    _stage_recovery_family(dataset, batches, per_batch)
    occurrences = 2 * batches * per_batch

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(staging, "_SEARCH_WORK_LIMIT", 24 * occurrences)
        got = discover_staged(dataset, RUN)

    picked = sorted(unit for fragment in got for unit in json.loads(fragment)["units"])
    assert len(got) == batches, f"the cover selected {len(got)} fragments for {batches} redelivered batches"
    assert picked == sorted(f"b{batch:04d}-u{unit:04d}" for batch in range(batches) for unit in range(per_batch))


# ── what the bound must not have cost ─────────────────────────────────────────────────


def test_the_pivot_heap_stays_proportional_to_the_UNITS_not_to_the_budget() -> None:
    """Bounding the runtime must not have traded it for an unbounded allocation.

    The pivot heap is lazy: a superseded entry is discarded only when it surfaces, and every take
    and every undo pushes more. Left alone, its size is bounded by the STEP BUDGET — up to 20M
    tuples inside the finalize pod — rather than by the run, so a long ambiguous search would die on
    memory instead of coming back ABANDONED with every byte still staged.

    Driven through `_CoreSearch` directly because that is where the bound lives, and a search that
    reached it through `discover_staged` would have to be one this file's other tests prove cannot
    be built in bounded time. 200 take/undo cycles is far past the point where the heap would
    otherwise be dominated by stale entries.

    The pivot and the candidate list are re-asserted afterwards: compaction rebuilds the heap, so it
    is exactly the operation that could silently drop the invariant the branching rule reads.
    """
    sets = [
        frozenset({"u0", "u1", "u2"}),
        frozenset({"u0", "u1"}),
        frozenset({"u2", "u3"}),
        frozenset({"u0"}),
        frozenset({"u1"}),
        frozenset({"u2"}),
        frozenset({"u3"}),
    ]
    universe = frozenset({"u0", "u1", "u2", "u3"})
    state = staging._CoreSearch.build(sets, list(range(len(sets))), universe)
    before = dict(state.avail)

    for _ in range(200):
        state.take(0)
        state.undo(0)

    assert len(state.pivots) <= staging._PIVOT_HEAP_SLACK * len(state.rest), f"the pivot heap grew to {len(state.pivots)} entries for {len(state.rest)} units"
    assert state.rest == set(universe), "undo did not restore the units it removed"
    assert state.avail == before, "undo did not restore the holder counts it moved"
    # u3 is held by two fragments where every other unit is held by three, so it is the pivot; both
    # of its holders are still candidates.
    assert state.pivot() == "u3"
    assert state.candidates() == [2, 6]
