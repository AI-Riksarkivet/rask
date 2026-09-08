"""Pruning runs must also reclaim the datasets it orphans, and must NOT reclaim a declared table.

§ Q8-15. The live sweep reports `storage_loss` and `unreadable` on every tick, and the cause is not a
data problem — it is test residue in the production lineage graph that nothing removes. `prune_runs` is
the only prune in the service and there is no dataset prune at all.

MEASURED ON THE LIVE ESTATE 2026-09-08, and it refutes the obvious fix:

    Dataset nodes   1271
    referenced_in   1271     every one has an incoming edge
    Run nodes       5692

So there is NO isolated node to reclaim — a "delete unreferenced nodes" sweep would free nothing. The
residue is reachable only THROUGH its runs, which makes dataset pruning the second half of run
retention rather than a sweep of its own: a dataset becomes prunable exactly when its last run is.

THE COST IS THE CONTROL, NOT THE DISK. The reconcile probes every node it holds, so the two warnings
fire each tick carrying dead rows, and a REAL storage loss arrives invisible among them — the estate's
"a control that cannot fire" pattern, reached by accumulation. Each dead node also costs a failed S3
list per tick inside the single-flight lock that gates the outbox drain.

THE SAFETY PROPERTY IS THE SECOND TEST. A table someone CREATED but nothing has written yet is a
declared table, not residue, and deleting it would erase the record that it was made.
"""

from __future__ import annotations

from lineage.services import cypher as cy


def test_the_orphan_query_asks_only_about_RUN_activity() -> None:
    """ "Has anything ever happened here" is the question a lineage graph answers. Both run edges must be
    counted, or the query reclaims a dataset a run still refers to."""
    for clause in ("[w:WROTE]-()", "[r:READ]-()"):
        assert clause in cy.COUNT_ORPHAN_DATASETS, f"the orphan count ignores {clause} — a live dataset would be pruned"
        assert clause in cy.PRUNE_ORPHAN_DATASETS_TEMPLATE, f"the orphan delete ignores {clause} — a live dataset would be deleted"


def test_a_DECLARED_table_is_not_residue() -> None:
    """The safety property. A `CREATED` edge means a person made this table and no run has touched it
    yet; deleting the node erases that fact, and the next catalog read finds a table the graph has never
    heard of."""
    for query in (cy.COUNT_ORPHAN_DATASETS, cy.PRUNE_ORPHAN_DATASETS_TEMPLATE):
        assert "[c:CREATED]-()" in query, "a declared-but-unwritten table would be pruned as residue"
        assert "nc = 0" in query, "the CREATED edge is matched but never required to be absent"


def test_the_query_avoids_the_AGE_SYNTAX_THAT_ALREADY_BROKE_IT() -> None:
    """THE PIN THAT WOULD HAVE CAUGHT THE REAL DEFECT, and it exists because it did not.

    This was first written as `WHERE NOT (d)<-[:WROTE]-(:Run)`. AGE 1.5.0 rejects a negated pattern
    predicate — `syntax error at or near ":"` — and no test saw it: these pins assert on the query
    STRING and never execute it, so the suite was green and the deployed prune failed on its first live
    tick with `lineage_dataset_prune_failed`.

    A structural test cannot check syntax, so it checks the FORM proved against the running database
    instead: `OPTIONAL MATCH` plus `count(...) = 0`, never a negated pattern.
    """
    for query in (cy.COUNT_ORPHAN_DATASETS, cy.PRUNE_ORPHAN_DATASETS_TEMPLATE):
        assert "OPTIONAL MATCH" in query, "the orphan query left the form AGE actually accepts"
        assert "NOT (" not in query, "a negated pattern predicate is back; AGE answers it with a syntax error, not a result"


def test_the_delete_is_batched_by_the_one_constant() -> None:
    """AGE 1.5.0 cannot bind LIMIT as a param, so the template interpolates it. One constant sizes both
    the loop and the delete — the drift `prune_runs` already paid for once."""
    assert "{limit}" in cy.PRUNE_ORPHAN_DATASETS_TEMPLATE, "the orphan delete is unbounded; a large backlog will exceed statement_timeout and roll back forever"
    assert "LIMIT {limit}" in cy.PRUNE_ORPHAN_DATASETS_TEMPLATE
    assert isinstance(cy.PRUNE_BATCH_SIZE, int) and cy.PRUNE_BATCH_SIZE > 0


def test_the_orphan_prune_only_runs_behind_retention() -> None:
    """It is the second half of run retention, not a standalone sweep. An estate that keeps everything
    must keep its dataset nodes too — otherwise the graph would drop nodes whose runs are still there."""
    import inspect

    from lineage.api import reconcile_cron

    src = inspect.getsource(reconcile_cron._prune_old_runs)
    guard = src.index("if not settings.run_retention_days")
    call = src.index("prune_orphan_datasets")
    assert guard < call, "the orphan prune is not behind the retention guard — a keep-everything estate would still lose nodes"
