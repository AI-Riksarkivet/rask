"""Retention reclaims a Job node once no run refers to it, so the Job population is not monotonic.

`prune_runs` removes old runs and `prune_orphan_datasets` removes the datasets they leave behind —
nothing removes the JOB. A Job whose every run has been pruned is unreachable and permanent, and it is
not merely disk: the `/jobs` governance fold makes a Job's output set its access handle, so the residue
is an access-control object for work nobody can see any more.

MEASURED ON THE LIVE GRAPH 2026-09-23: **3,374 Job nodes, 111 of them with no run at all.** The count
can only grow, because the mechanism that creates the condition is retention itself.

THE QUERY SHAPE IS NOT A STYLE CHOICE. AGE 1.5.0 rejects a negated pattern predicate, so
`WHERE NOT (j)<-[:OF_JOB]-(:Run)` is a syntax error at the `:` — confirmed against the running database
before this was written (`syntax error at or near ":"`), the same trap `_ORPHAN_DATASETS` records. The
`OPTIONAL MATCH` + count form below was executed there first and answered 111. These pins assert on the
query STRING and never run it, which is exactly how a prune once shipped green and failed on its first
live tick — so the proof is the live run, and this file only stops the shape drifting back.
"""

from __future__ import annotations

import lineage.services.cypher as cy


def test_the_orphan_job_query_uses_the_form_AGE_accepts() -> None:
    """A negated pattern predicate parses nowhere and is caught by no test that does not execute it."""
    assert "WHERE NOT (j)<-[:OF_JOB]-" not in cy.COUNT_ORPHAN_JOBS
    assert "OPTIONAL MATCH (j)<-[o:OF_JOB]-()" in cy.COUNT_ORPHAN_JOBS
    assert "count(o)" in cy.COUNT_ORPHAN_JOBS


def test_a_job_is_orphaned_only_when_NO_run_refers_to_it() -> None:
    """The condition is zero runs. Anything weaker reclaims a Job whose history is still readable."""
    assert "WHERE no = 0" in cy.COUNT_ORPHAN_JOBS


def test_the_prune_is_batched_like_its_siblings() -> None:
    """One transaction per batch, so a large backlog cannot push a statement past the pool's timeout."""
    statement = cy.PRUNE_ORPHAN_JOBS_TEMPLATE.format(limit=cy.PRUNE_BATCH_SIZE)
    assert f"LIMIT {cy.PRUNE_BATCH_SIZE}" in statement
    assert statement.rstrip().endswith("DETACH DELETE j")


def test_the_count_and_the_delete_select_the_SAME_jobs() -> None:
    """Two spellings of "orphan" is how a prune deletes something the count never promised."""
    shared = cy.COUNT_ORPHAN_JOBS.split("RETURN")[0]
    assert cy.PRUNE_ORPHAN_JOBS_TEMPLATE.startswith(shared)
