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


def test_the_count_and_the_delete_ask_the_same_question() -> None:
    """Identity, not resemblance — and it is load-bearing arithmetic, not tidiness.

    ``prune_runs`` sizes its batch loop as ``ceil(count / PRUNE_BATCH_SIZE)`` and then runs the delete
    that many times. If the count matched a wider set than the delete, the loop would run extra empty
    batches; if it matched a narrower one, runs old enough to prune would survive every tick and the
    retention window would silently stop meaning what the chart says. One constant behind both is what
    makes the two impossible to disagree.
    """
    assert cy.COUNT_OLD_RUNS.startswith(cy._PRUNABLE_RUNS)
    assert cy.PRUNE_OLD_RUNS_TEMPLATE.startswith(cy._PRUNABLE_RUNS)


def test_the_pruner_exempts_a_dataset_whose_only_writer_is_the_run_being_pruned() -> None:
    """The exemption itself: keep a run when some dataset it wrote has exactly one writer.

    ``min(writers) <> 1`` is the whole rule. A count of 1 can only be this run itself, so 1 means sole
    provenance; 0 means the run wrote nothing and >= 2 means another run still speaks for every dataset
    it touched. Asserted on the predicate rather than on either query, because both inherit it.
    """
    predicate = cy._PRUNABLE_RUNS

    assert "OPTIONAL MATCH (r)-[:WROTE]->(d:Dataset)" in predicate, "the exemption has to know what this run wrote"
    assert "OPTIONAL MATCH (d)<-[w:WROTE]-(:Run)" in predicate, "and how many other runs wrote the same dataset"
    assert "min(writers) AS fewest" in predicate, "the smallest per-dataset writer count is what decides it"
    assert "WHERE fewest <> 1" in predicate, "a sole-provenance run must be KEPT, not pruned"


def test_the_pruner_still_bounds_itself_by_age() -> None:
    """The exemption narrows retention; it must not replace it.

    Without the age predicate the query would exempt-or-delete the whole graph on every tick. Pinned
    separately from the exemption so a change to one cannot quietly remove the other.
    """
    assert "r.event_time < $cutoff" in cy._PRUNABLE_RUNS


def test_the_exemption_does_not_use_a_negated_pattern_predicate() -> None:
    """AGE 1.5.0 rejects `WHERE NOT (d)<-[:WROTE]-(:Run)` as a syntax error at the `:`.

    The natural way to write "no other run wrote this" is the way that does not parse, and it fails on
    the server rather than in any suite — which is how the orphan query beside this one shipped broken.
    Pinned as a NEGATIVE so the next person to simplify this predicate is stopped by a test rather than
    by a production tick.
    """
    assert "NOT (" not in cy._PRUNABLE_RUNS


def test_the_batch_limit_is_still_applied_after_the_exemption() -> None:
    """The LIMIT must come after the filter, or a batch could be entirely exempt and delete nothing.

    With the filter first, each batch takes `PRUNE_BATCH_SIZE` genuinely deletable runs and the loop
    terminates. With it after, a tick could spin its whole batch budget on runs it then refuses to
    delete, and the backlog would never drain.
    """
    delete = cy.PRUNE_OLD_RUNS_TEMPLATE.format(limit=cy.PRUNE_BATCH_SIZE)

    assert delete.index("WHERE fewest <> 1") < delete.index(f"LIMIT {cy.PRUNE_BATCH_SIZE}")
