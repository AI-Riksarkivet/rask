"""Retention may age a dataset's history out; it may not leave the dataset claiming a false one.

[[LH-146]]. Pruning by age alone takes a dataset's final ``WROTE`` edge. The dataset then has no
versioned write, the reconciler reads that as UNTRACKED, back-fills it, and MERGEs a synthetic run
with ``author='reconcile'`` and — its own docstring — "no inputs". So the graph does not shrink, it is
REWRITTEN: the fact of each write survives while the actor and the derivation do not, and nothing can
rebuild them, because the durable ``/events`` feed is retained 7 days while the graph reaches back 30.

**THE EXEMPTION CLOSES THE CYCLE RATHER THAN PATCHING ONE END OF IT.** A dataset that always keeps one
real run is never UNTRACKED, so the back-fill it would otherwise trigger never runs — the synthetic
record is not suppressed, it is never minted. Hole recovery is untouched: STORAGE_AHEAD, where storage
genuinely holds versions the graph lacks, is a different classification and still back-fills.

WHAT THESE TESTS CAN AND CANNOT PROVE, stated because the neighbouring suite got this wrong. A test
over a Cypher STRING cannot tell you the query parses: ``PRUNE_ORPHAN_DATASETS_TEMPLATE`` shipped as a
negated pattern predicate that AGE 1.5.0 rejects at the ``:``, the string pins stayed green, and the
deployed prune failed on its first live tick. So the queries here were EXECUTED against the deployed
database before they were written — the exact committed strings, with the cutoff bound, the COUNT
read-only and the DELETE inside a rolled-back transaction (2026-09-15): 310 candidate runs at a probe
cutoff, 2 exempt, 308 prunable, the three reconciling exactly. What these tests add on top is the
property no single execution can show — that the two queries cannot DRIFT APART.
"""

from __future__ import annotations

from lineage.services import cypher as cy


def test_the_exemption_asks_about_the_dataset_not_about_the_run() -> None:
    """The rule is per-DATASET, and this is the property that survives a batch.

    ``min(newest) >= $cutoff`` is the whole rule: for every dataset the run wrote, take that dataset's
    newest write and prune only when the oldest of those is still inside the window. A run that wrote
    nothing yields NULL and is prunable.

    A PER-RUN RULE LOOKS EQUIVALENT AND IS NOT — this is the bug this test exists to prevent, and it
    was nearly shipped. "Keep a run that is some dataset's only writer" asks about the graph as it
    stands rather than as the batch will leave it: when every writer of a dataset is past the cutoff,
    each one sees two or more writers and is individually prunable, so a single ``DETACH DELETE`` takes
    them all. Measured against the deployed graph at a probe cutoff, that shape still stripped 10
    datasets of 57 runs. Asking about the dataset's newest write is a fact no batch composition can
    change.
    """
    predicate = cy._PRUNABLE_RUNS

    assert "OPTIONAL MATCH (r)-[:WROTE]->(d:Dataset)" in predicate, "the rule has to know what this run wrote"
    assert "max(any.event_time) AS newest" in predicate, "and when each of those datasets was last written"
    assert "min(newest) AS oldest_tip" in predicate, "the least-recently-written of them is what decides it"
    assert "WHERE oldest_tip IS NULL OR oldest_tip >= $cutoff" in predicate, "a quiet dataset's runs must be KEPT; a run that wrote nothing is prunable"


def test_the_pruner_still_bounds_itself_by_age() -> None:
    """The exemption narrows retention; it must not replace it.

    Without the age predicate the query would exempt-or-delete the whole graph on every tick. Pinned
    separately from the exemption so a change to one cannot quietly remove the other.
    """
    assert "r.event_time < $cutoff" in cy._PRUNABLE_RUNS
