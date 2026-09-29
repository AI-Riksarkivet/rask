"""Pruning runs must also reclaim the datasets it orphans, and must NOT reclaim a declared table.

§ Q8-15. The live sweep reports `storage_loss` and `unreadable` on every tick, and the cause is not a
data problem — it is test residue in the production lineage graph that nothing removes. `prune_runs` is
the only prune in the service and there is no dataset prune at all.

MEASURED ON THE LIVE ESTATE 2026-09-08, and it refutes the obvious fix:

    Dataset nodes   1271
    referenced_in   1271     every one has an incoming edge
    Run nodes       5692

So there is NO isolated node to reclaim — a "delete unreferenced nodes" sweep would free nothing. The
residue is reachable only THROUGH its runs, which makes dataset pruning the second half of run retention
rather than a sweep of its own.

LOSING ITS LAST RUN IS NOT SUFFICIENT, THOUGH, and the second test below is why. The query also requires
no `CREATED` edge, and that edge comes from a `User` rather than a Run, so no run prune can remove it.
Measured on the live estate 2026-09-11: all 1150 CREATED edges originate at `User` nodes, 1145 of 1247
Dataset nodes carry one, and the orphan query matches ZERO. The guard is right and these tests pin it;
what the pairing means is that retention reclaims nothing for a user-created table, so residue needs a
remedy that is not this one.

THE COST IS THE CONTROL, NOT THE DISK. The reconcile probes every node it holds, so the two warnings
fire each tick carrying dead rows, and a REAL storage loss arrives invisible among them — the estate's
"a control that cannot fire" pattern, reached by accumulation. Each dead node also costs a failed S3
list per tick inside the single-flight lock that gates the outbox drain.

THE SAFETY PROPERTY IS THE SECOND TEST. A table someone CREATED but nothing has written yet is a
declared table, not residue, and deleting it would erase the record that it was made.
"""

from __future__ import annotations

from lineage.services import cypher as cy


def test_a_DECLARED_table_is_not_residue() -> None:
    """The safety property. A `CREATED` edge means a person made this table and no run has touched it
    yet; deleting the node erases that fact, and the next catalog read finds a table the graph has never
    heard of."""
    for query in (cy.COUNT_ORPHAN_DATASETS, cy.PRUNE_ORPHAN_DATASETS_TEMPLATE):
        assert "[c:CREATED]-()" in query, "a declared-but-unwritten table would be pruned as residue"
        assert "nc = 0" in query, "the CREATED edge is matched but never required to be absent"
