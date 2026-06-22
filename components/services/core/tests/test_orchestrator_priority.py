"""HTR eligible-chunk prioritisation policy (orchestrator scheduling fairness).

Regression guard for the starvation bug: a handful of low-id chunks whose
transcript count plateaus below `expected_pages` stayed permanently eligible and,
served front-of-an-ascending-list into the inflight slots, reclaimed every freed
slot — so 92 of 100 chunks were never submitted once. The policy under test:
never-run chunks (no transcripts yet) come first; partial chunks fill remaining
slots in random order so no single chunk is always the one re-run.
"""

from core.services.orchestrator.derive import _prioritize_htr


def test_no_work_chunks_come_before_partial_chunks():
    eligible = [0, 1, 2, 8, 9, 10]
    transcribed = {0: 6891, 1: 7094, 2: 7035, 8: 0, 9: 0, 10: 0}
    ordered = _prioritize_htr(eligible, transcribed)

    no_work = {8, 9, 10}
    first_partial = next(i for i, c in enumerate(ordered) if c not in no_work)
    # every no-work chunk appears before the first partial one
    assert set(ordered[:first_partial]) == no_work
    assert set(ordered) == set(eligible)  # nothing dropped, nothing duplicated


def test_all_partial_returns_full_set():
    eligible = [0, 1, 2, 3]
    transcribed = {0: 10, 1: 20, 2: 30, 3: 40}  # all have work
    ordered = _prioritize_htr(eligible, transcribed)
    assert sorted(ordered) == eligible  # a permutation, nothing lost


def test_missing_chunk_counts_as_no_work():
    # a chunk absent from the map (never reconciled) is treated as zero transcripts
    eligible = [5, 7]
    ordered = _prioritize_htr(eligible, {7: 100})
    assert ordered[0] == 5  # the unknown/zero chunk is prioritised


def test_empty_eligible_is_empty():
    assert _prioritize_htr([], {}) == []
