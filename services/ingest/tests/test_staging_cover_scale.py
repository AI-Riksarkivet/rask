"""The finalizer's exact-cover search, at the scale the ingest plane advertises.

`discover_staged` is the single commit point of a run: `finalize_run` calls it after every byte has
been fetched, validated, written as a Lance fragment and ACKED OFF the queue. So a failure here is
not merely slow — the units are gone from a WORK_QUEUE stream, the workflow's error boundary writes
`status="FAILED"`, and a retry replays the same deterministic failure. There is no cheap recovery.

Nothing else in this suite exercises that path at more than a handful of fragments, which is why two
scale defects lived in it undetected. Both are about the BENIGN input — a clean run whose batches
share no units at all, which is what every uncrashed run stages:

  * the search rebuilt a candidate index over the WHOLE remaining unit universe at every node, so
    cost grew super-linearly in the run's size rather than linearly in its staged manifests;
  * it recursed once per fragment it picked, so a clean run's stack depth equalled its manifest
    count — around 950 manifests, i.e. a ~970k-unit run at the default `fragment_rows=1024`, blows
    CPython's default limit.

Both surface as the SAME operator-visible symptom, and it is the sharpest harm in the finding: a
perfectly clean run refused at the very end. The tests below therefore assert on the benign case
only. The genuinely-conflicting case is pinned by `test_partial_ack_duplication.py`, and must stay
exactly as loud as it is.
"""

from __future__ import annotations

import json
import random
import sys
import time
from itertools import combinations
from pathlib import Path

import pytest

from ingest import staging
from ingest.staging import StagingCoverAbandoned, StagingOverlapError, discover_staged, stage_fragments


RUN = "run-cover-scale"


def _stack_depth() -> int:
    """Frames currently on the stack. Used to set a recursion limit RELATIVE to pytest's own depth.

    An absolute limit would be a guess: the test's base depth depends on how pytest was invoked
    (plugins, `-p no:randomly`, assertion rewriting), so a hard-coded 100 could leave either no
    headroom at all or more than the test intends to allow.
    """
    depth = 0
    frame: object | None = sys._getframe()
    while frame is not None:
        depth += 1
        frame = getattr(frame, "f_back", None)
    return depth


def test_the_cover_finishes_a_run_a_hundredth_the_advertised_scale(tmp_path: Path) -> None:
    """400 pairwise-disjoint batches — 25,600 units, ~0.25% of the corpus this plane advertises.

    Disjoint is the ordinary shape: no crash, no partial ack, every batch owning its own units. The
    only exact cover of that input is "all of them", and finding it should cost one linear pass over
    the staged unit occurrences.

    The bound is wall-clock rather than a call count because the defect is not a wrong answer — the
    old search returns the right fragments, eventually. What made it a data-loss risk is that
    "eventually" grows with the RUN's size, on the code path that runs after the units are already
    acked. 5s for 0.25% of the advertised scale is generous by orders of magnitude and still fails a
    solver whose per-node work is proportional to the universe.
    """
    dataset = str(tmp_path / "bronze")
    batches = 400
    per_batch = 64

    for batch in range(batches):
        units = [f"u{batch:04d}-{i:03d}" for i in range(per_batch)]
        stage_fragments(dataset, RUN, units, [f'{{"n":{batch}}}'])

    start = time.perf_counter()
    got = discover_staged(dataset, RUN)
    elapsed = time.perf_counter() - start

    assert len(got) == batches, f"the cover selected {len(got)} of {batches} disjoint fragments"
    assert elapsed < 5.0, f"the exact cover took {elapsed:.1f}s on {batches} disjoint fragments ({batches * per_batch} units)"


def test_a_clean_cover_does_not_consume_a_stack_frame_per_fragment(tmp_path: Path) -> None:
    """Selecting N fragments must not cost N stack frames.

    A recursive solver that recurses once per PICKED fragment ties its stack depth to the run's
    manifest count, and in the disjoint case exactly one fragment is picked per level — so depth ==
    manifest count exactly. At CPython's default 1000-frame limit, and with the frames
    `finalize_run` and the Dapr activity dispatch already hold, that puts the ceiling at roughly 950
    clean manifests: a ~970k-unit run at the default `fragment_rows=1024`. Under the plane's own
    advertised scale, and reached by a run that did nothing wrong.

    The limit is lowered rather than the input being grown to ~1000 fragments, because at that size
    the OTHER defect (per-node work proportional to the universe) dominates and the assertion would
    take minutes to fail for the wrong reason. Lowering the limit isolates the stack cost: 300
    fragments against 60 frames of headroom is red iff the solver recurses per fragment, and green
    for any solver with a bounded frame cost — it would stay green at 30,000 fragments.
    """
    dataset = str(tmp_path / "bronze")
    fragments = 300

    for index in range(fragments):
        stage_fragments(dataset, RUN, [f"u{index:04d}"], [f'{{"n":{index}}}'])

    original = sys.getrecursionlimit()
    sys.setrecursionlimit(_stack_depth() + 60)
    try:
        got = discover_staged(dataset, RUN)
    finally:
        sys.setrecursionlimit(original)

    assert len(got) == fragments, f"the cover selected {len(got)} of {fragments} disjoint fragments"


# ── what making it fast must not cost ─────────────────────────────────────────────────


def _a_cover_exists(sets: list[frozenset[str]], universe: frozenset[str]) -> bool:
    """Brute force: is there ANY subset of these fragments covering every unit exactly once?

    Exponential and deliberately so. Scoring a solver with anything that shares its reasoning proves
    nothing, and the rewrite below replaced that reasoning wholesale — plain backtracking became an
    owner index plus unit propagation plus a search over what is left. Only an independent oracle can
    say the answers did not move.
    """
    for size in range(1, len(sets) + 1):
        for combo in combinations(sets, size):
            covered: set[str] = set()
            for unit_set in combo:
                if unit_set & covered:
                    break
                covered |= unit_set
            else:
                if covered == universe:
                    return True
    return False


def test_the_faster_solver_answers_exactly_what_the_brute_force_oracle_does(tmp_path: Path) -> None:
    """Speed must not have moved a single verdict, in either direction.

    Unit propagation is what makes the clean case linear, and it works by DECIDING fragments before
    any search runs. That is sound — a fragment that is the only holder of some unit belongs to every
    exact cover — but it is sound only as an argument, so it is checked against brute force over
    inputs a partially-acked run actually produces.

    Wider than the fuzz in `test_partial_ack_duplication.py`, on purpose: that one uses 2-4 fragments
    over 6 units, which lands almost entirely in the search. Six to twelve fragments over 10 units
    reaches the propagation paths as well, and mixing in singletons reproduces the shape
    `drain_chunk`'s singleton-redelivery rule stages.

    Both directions are asserted. A solver that never refuses anything would pass a "does not refuse
    resolvable work" test while committing units twice, so every accepted answer is also checked to
    be a real exact cover.
    """
    # Seeded: the value of a fuzz here is the orderings a hand-picked case would not think of, and an
    # unreproducible failure in a data-loss path cannot be debugged.
    rng = random.Random(20260828)  # noqa: S311 — shuffling test inputs, not minting secrets
    universe_keys = [f"u{i}" for i in range(10)]
    resolvable = 0
    unresolvable = 0

    for case in range(300):
        sets = [frozenset(rng.sample(universe_keys, rng.randint(1, 5))) for _ in range(rng.randint(3, 6))]
        # Redeliveries reach staging as singletons, so a realistic family carries some.
        sets += [frozenset([rng.choice(universe_keys)]) for _ in range(rng.randint(0, 3))]
        sets = list(dict.fromkeys(sets))  # a manifest is named by its unit set, so duplicates cannot co-exist
        universe = frozenset().union(*sets)

        dataset = str(tmp_path / f"case{case}")
        for index, unit_set in enumerate(sets):
            units = sorted(unit_set)
            stage_fragments(dataset, RUN, units, [json.dumps({"n": index, "units": units})])

        expected = _a_cover_exists(sets, universe)
        try:
            staged = discover_staged(dataset, RUN)
        except StagingOverlapError:
            assert not expected, f"case {case}: refused an input the oracle resolves — {sorted(sorted(s) for s in sets)}"
            unresolvable += 1
            continue

        assert expected, f"case {case}: accepted an input the oracle says has no exact cover — {sorted(sorted(s) for s in sets)}"
        resolvable += 1
        picked = [unit for fragment in staged for unit in json.loads(fragment)["units"]]
        assert sorted(picked) == sorted(universe), f"case {case}: the selection does not cover the run — {picked}"
        assert len(picked) == len(set(picked)), f"case {case}: a unit would commit twice — {picked}"

    assert resolvable > 50, f"only {resolvable} resolvable cases — the fuzz is testing nothing"
    assert unresolvable > 10, f"only {unresolvable} unresolvable cases — the refusal path is untested"


def test_a_fragment_covering_NO_unit_is_still_never_committed(tmp_path: Path) -> None:
    """A manifest with fragments but an empty unit set claims no rows, so it must not land.

    It was excluded before by accident — it was a candidate for no unit, so backtracking never
    reached it. Propagation has no such accident available: a naive "nothing is contested, take
    everything" pass would commit rows that no unit in the run accounts for, which is the duplication
    this module exists to prevent wearing a different shape.
    """
    dataset = str(tmp_path / "bronze")
    root = Path(staging.staging_root(dataset, RUN))
    root.mkdir(parents=True)
    (root / "orphan.json").write_text(json.dumps({"units": [], "fragments": ['{"n":"orphan"}']}), encoding="utf-8")
    stage_fragments(dataset, RUN, ["u0"], ['{"n":"real"}'])

    assert discover_staged(dataset, RUN) == ['{"n":"real"}']


def test_giving_up_is_reported_as_giving_up_NOT_as_an_overlap(tmp_path: Path) -> None:
    """The finding's sharpest harm: a clean run told its fragments conflict.

    Both outcomes used to arrive as a bare `None` from the solver and were rendered as the same
    `StagingOverlapError`. They demand opposite responses — an overlap needs fragments reconciled by
    hand, an abandoned search needs the finalize re-run — so an operator who cannot tell them apart
    has no correct next move.

    The budget is lowered rather than an input built to exhaust the real one, because an input that
    exhausts 20M steps is by construction one the other tests in this file prove cannot be built in
    bounded time. What is under test is the REPORTING, and lowering the budget reaches it exactly.

    The input has to survive propagation to reach the search at all: F={u0,u1} with both units also
    staged as singletons leaves every unit with two holders, so nothing is forced.
    """
    dataset = str(tmp_path / "bronze")
    stage_fragments(dataset, RUN, ["u0", "u1"], ['{"n":"F","units":["u0","u1"]}'])
    stage_fragments(dataset, RUN, ["u0"], ['{"n":"G","units":["u0"]}'])
    stage_fragments(dataset, RUN, ["u1"], ['{"n":"H","units":["u1"]}'])

    # Resolvable, and resolved, under the shipped budget.
    assert discover_staged(dataset, RUN) == ['{"n":"F","units":["u0","u1"]}']

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(staging, "_SEARCH_WORK_LIMIT", 1)
        with pytest.raises(StagingCoverAbandoned) as abandoned:
            discover_staged(dataset, RUN)

    assert not isinstance(abandoned.value, StagingOverlapError), "an abandoned search must not be catchable as an overlap"
    assert "ABANDONED" in str(abandoned.value)
    assert "not a verdict that they overlap" in str(abandoned.value)


def test_a_GENUINE_conflict_is_still_refused_as_an_overlap(tmp_path: Path) -> None:
    """Splitting the abandoned case off must not have softened the real refusal.

    F={u0..u3} against H={u2..u5}: each holds rows the other lacks, so committing both duplicates
    u2/u3 and committing either alone loses two units. The refusal must still be a
    `StagingOverlapError` and must still NAME the stranded units — an operator cannot act on "some
    overlap somewhere", and the named units are what tells them which fragments to reconcile.

    Also asserts the mechanism, not just the class: propagation reaches this verdict in linear time
    (u4 and u5 are held only by H, so H is forced, which rules out F and leaves u0/u1 unheld), and it
    must not be reported as having run out of budget.
    """
    dataset = str(tmp_path / "bronze")
    stage_fragments(dataset, RUN, ["u0", "u1", "u2", "u3"], ['{"n":"F"}'])
    stage_fragments(dataset, RUN, ["u2", "u3", "u4", "u5"], ['{"n":"H"}'])

    with pytest.raises(StagingOverlapError, match="u4"):
        discover_staged(dataset, RUN)
