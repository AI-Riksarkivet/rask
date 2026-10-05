"""CONTRACT (LH-003, condition 1): a write on a BRANCH says so in its lineage event.

THE COLLISION, reproduced on the installed pylance rather than argued. A branch is a whole parallel
dataset under `tree/<name>/` with its OWN version sequence (`catalog/core/namespace.py::open_dataset`
states it, and `checkout_version((branch, None))` is how the catalog reaches one). Driven locally:

    main            v2  rows=3
    branch write    v3  rows=4   <- the branch
    later main write v3 rows=4   <- main, DIFFERENT contents

Same dataset, same version NUMBER, two different snapshots. The WROTE edge records only the number, so
the graph cannot tell them apart — and `LATEST_WRITE_VERSION` orders by `event_time DESC` alone, so the
most recent write WINS regardless of which ref it landed on.

The producer half (every branch door's event names its ref) is driven end to end by
`tests/integration/test_a_write_event_names_the_commit_it_made.py`; this file holds the consumer half.
"""

from __future__ import annotations


def test_the_reads_that_answer_MAINS_version_exclude_branch_writes() -> None:
    """The consumer half: storing the ref buys nothing unless the reads use it.

    `LATEST_WRITE_VERSION` answers "what version is this table at" and `SCHEMA_LATEST` answers "what is
    its schema" — both ordered by `event_time DESC` alone, so the most recent write won whichever ref it
    landed on. `core/reconcile.py` then compares that number against MAIN's on-disk version, so a branch
    write could be classified as main drift and back-filled.

    `w.ref IS NULL` is the filter rather than `w.ref = 'main'`, and that is load-bearing for history:
    every write recorded before the ref existed carries no property, and those were all main writes.
    """
    from lineage.services import cypher as cy

    for name, statement in (("LATEST_WRITE_VERSION", cy.LATEST_WRITE_VERSION), ("SCHEMA_LATEST", cy.SCHEMA_LATEST)):
        assert "w.ref IS NULL" in statement, f"{name} still reports a branch write as main's: {statement}"
    assert "SET w.ref=$ref" in cy.SET_WROTE_REF, cy.SET_WROTE_REF
    # Its OWN statement, like the version beside it: AGE silently drops a $param in a SET fused to an
    # edge MERGE, so a ref written that way would be stored as null with no error.
    assert "MERGE" not in cy.SET_WROTE_REF, "the ref is fused to a MERGE — AGE would drop the parameter"
