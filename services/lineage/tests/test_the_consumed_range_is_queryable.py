"""The source version a run CONSUMED survives into the run board, and survives a later bare event.

`open_cascade_repair.md` C3b. `498b5531` put `from_version`/`to_version` into the `lance` run facet, so
the cascade's delta boundary is finally recorded — and it is still unqueryable, which is one layer
further along than the gap that commit closed. `RunStatus` folds `operation`, `source_run_id` and
`promotion_status` off that facet and not the range, so nothing can answer *"what source version has
silver actually consumed?"* — which is exactly the predicate the cascade lag detector needs.

STICKINESS IS THE WHOLE DIFFICULTY, and the three fields beside it already say why: a reconcile or
backfill event for the same graph run carries NO lance facet, and a later bare event that clobbered the
value would erase what an earlier one declared. `operation` and `source_run_id` and `promotion_status`
each use `CASE WHEN $x = '' THEN <keep> ELSE $x END` for that reason. A version is an INT, so the empty
string is not available as "the event did not say" — the sentinel has to be a value a version can never
take.
"""

from __future__ import annotations

from lineage.models import RunEvent


def _event(**lance: object) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-04T06:00:00Z",
            "producer": "test",
            "run": {"runId": "11111111-1111-5111-8111-111111111111", "facets": {"lance": {"_producer": "t", **lance}}},
            "job": {"namespace": "medallion", "name": "embed_features"},
            "outputs": [{"namespace": "silver", "name": "silver$features"}],
        }
    )


def test_the_consumed_ceiling_is_read_off_the_facet() -> None:
    assert _event(operation="embed_features", to_version=7).consumed_to_version == 7


def test_a_run_that_declares_no_range_reads_None() -> None:
    """A full rescan and a promotion carry no range. None, never 0 — 0 is a real version."""
    assert _event(operation="embed_features").consumed_to_version is None


def test_a_non_integer_is_refused_rather_than_coerced() -> None:
    """The facet is producer-supplied. A string that happens to parse would let an external producer
    write a version into the graph that no catalog ever published."""
    assert _event(operation="x", to_version="7").consumed_to_version is None
    assert _event(operation="x", to_version=None).consumed_to_version is None


def test_a_negative_version_is_refused() -> None:
    """`-1` is the sentinel the Cypher SET uses for "this event did not say", so a producer must not be
    able to send it as data and silently mean "keep the old value"."""
    assert _event(operation="x", to_version=-1).consumed_to_version is None


def test_the_run_node_SET_keeps_a_value_a_later_bare_event_does_not_carry() -> None:
    """The property the three sibling fields each needed their own comment to explain.

    A reconcile or backfill event for the same graph run carries no `lance` facet at all. If the SET
    wrote `-1` over the stored value, the cascade's delta boundary would be erased on the next
    reconcile tick — the one fact the lag detector reads. Asserted against the Cypher text because the
    behaviour lives there, not in Python: a unit test of the repository would exercise a driver, not
    the statement AGE actually runs.
    """
    from lineage.services.cypher import MERGE_RUN

    assert "r.consumed_to_version=(CASE WHEN $ctv < 0 THEN r.consumed_to_version ELSE $ctv END)" in MERGE_RUN, (
        "the run-node SET no longer preserves consumed_to_version across an event that carries no range"
    )


def test_the_board_query_returns_it() -> None:
    """Folded onto the node and never selected is the same defect as not folded at all."""
    from lineage.services.cypher import LIST_RUNS

    assert "r.consumed_to_version" in LIST_RUNS, "LIST_RUNS does not return the consumed range, so the board cannot show it"
