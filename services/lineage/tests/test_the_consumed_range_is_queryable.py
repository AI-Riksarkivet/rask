"""The source version a run CONSUMED survives into the run board, and survives a later bare event.

docs/DECISIONS.md "Cascade repair" (C3b). `498b5531` put `from_version`/`to_version` into the `lance` run facet, so
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


def test_the_consumed_FLOOR_is_read_off_the_facet() -> None:
    """The other end of the range, and the half the lag detector could not see without it.

    `events.py` has stamped `from_version` into the `lance` facet since `498b5531` — the same builder,
    the same event, one key along from the ceiling. Only the ceiling was ever folded onto the run node,
    so a consumer could ask "how far did this run read TO" and never "did it start where the last one
    stopped". A cascade's loss lives in exactly that difference: a lost trigger's rows fall outside
    every later hop's filter, so the ceiling keeps climbing while the gap stays open.
    """
    assert _event(operation="embed_features", from_version=3, to_version=7).consumed_from_version == 3


def test_a_FIRST_publication_reads_None_not_zero() -> None:
    """`from_version` is absent on a first publication and means "everything up to `to`". Zero would
    assert a prior publication at version 0 that did not happen — the distinction `build_stage_trigger`
    carries on the wire and `consumed_frontier` reads as "covers from the start"."""
    assert _event(operation="embed_features", to_version=7).consumed_from_version is None


def test_the_floor_refuses_the_same_shapes_the_ceiling_does() -> None:
    """Producer-supplied, so a string that merely parses must not become a version, and `-1` is the
    Cypher sentinel for "this event did not say" — sendable as data it would mean "keep the old
    value", which is a producer editing the graph's memory rather than reporting its own run."""
    assert _event(operation="x", from_version="3", to_version=7).consumed_from_version is None
    assert _event(operation="x", from_version=-1, to_version=7).consumed_from_version is None
    assert _event(operation="x", from_version=True, to_version=7).consumed_from_version is None


def test_the_floor_is_STICKY_like_every_other_facet_field() -> None:
    """A reconcile or backfill event for the same graph run carries no lance facet. Clobbering the
    floor to null would erase the range's lower bound and make a contiguous history read as a gap —
    turning the loss detector into a permanent false alarm."""
    from lineage.services.cypher import MERGE_RUN

    assert "r.consumed_from_version=(CASE WHEN $cfv < 0 THEN r.consumed_from_version ELSE $cfv END)" in MERGE_RUN


def test_the_floor_comes_back_from_the_run_board() -> None:
    """Stored and unreadable is the state this whole file exists to end. `LIST_RUNS` is what the lag
    detector reads, so a column absent there is a field that does not exist as far as it is concerned."""
    from lineage.services.cypher import LIST_RUNS

    assert "r.consumed_from_version" in LIST_RUNS


def test_the_column_COUNT_matches_what_LIST_RUNS_returns() -> None:
    """AGE demands an explicit column-definition list, and a mismatch is a 500, not a short row.

    `run_cypher(..., columns=N)` builds `AS (col0 agtype, ... colN-1 agtype)`. Postgres answers
    `DatatypeMismatch: return row and column definition list do not match` when N disagrees with the
    RETURN — so adding a column to the query without updating the caller takes the whole run board
    down rather than degrading. Measured live 2026-09-05: every `/runs` read answered 500 and the lag
    detector counted 14 edges FAILED.

    Counted from the query itself so the two cannot drift again: a future column is added in one place
    and this gate names the other.

    THE MISMATCH PREDATES THIS FIELD. At `5c11002c` the query returned 15 columns and the caller
    declared 14, so `/runs` was already unservable — invisible because its only caller was refused by
    the service door first, and a 401 is returned before a query runs. Two defects stacked in one
    request path, the outer one hiding the inner.
    """
    import inspect
    import re

    from lineage.services import repository
    from lineage.services.cypher import LIST_RUNS

    returned = len(re.findall(r"\br\.[a-z_]+", LIST_RUNS))
    declared = {int(m) for m in re.findall(r"cy\.LIST_RUNS,\s*columns=(\d+)", inspect.getsource(repository))}
    assert declared == {returned}, f"LIST_RUNS returns {returned} columns and the caller declares {declared or 'none'}"
