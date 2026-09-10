"""The batch id that threads a cascade reaches the graph, so its runs can actually be joined.

`ingest_trigger.py` mints the batch identity where the cascade begins and says why in as many words:
*"Every tier below carries this same id, so the runs of one batch are joinable in the graph instead of
three unrelated hops sharing only a dataset name."* `schemas/events.py` then stamps it on every run
event as the `lance` facet's `cascade_id`.

MEASURED 2026-09-10: the graph stores none of it. `services/lineage` matches `cascade_id` in zero
files, `MERGE_RUN` writes `operation`, `source_run_id` and `promotion_status` off that same facet and
not this one, and `LIST_RUNS` returns no such column. So each hop lands as an isolated `Run` node and
the question the id exists to answer — *which runs belong to this batch* — cannot be asked. Two
producers agree on a contract the consumer never implemented, which is why nothing was red.

STICKINESS IS THE SAME DIFFICULTY THE THREE FIELDS BESIDE IT HAVE, and it is why this is not a one-line
change: a reconcile or backfill event for the same graph run carries NO lance facet, so a later bare
event that wrote its empty value would erase the batch id an earlier one declared. `operation`,
`source_run_id` and `promotion_status` each guard with `CASE WHEN $x = '' THEN <keep> ELSE $x END`, and
this takes the same guard for the same reason.
"""

from __future__ import annotations

from lineage.models import RunEvent


def _event(**lance: object) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-10T06:00:00Z",
            "producer": "test",
            "run": {"runId": "22222222-2222-5222-8222-222222222222", "facets": {"lance": {"_producer": "t", **lance}}},
            "job": {"namespace": "medallion", "name": "embed_features"},
            "outputs": [{"namespace": "silver", "name": "silver$features"}],
        }
    )


def test_the_batch_id_is_read_off_the_facet() -> None:
    assert _event(operation="embed_features", cascade_id="a1b2c3d4e5f6").cascade_id == "a1b2c3d4e5f6"


def test_a_run_that_declares_no_batch_reads_None() -> None:
    """An external OpenLineage producer, a catalog `register_table` marker and every reconcile event
    carry no batch. None, never `""` — an empty string is a batch id that joins to other empty ones."""
    assert _event(operation="create_table").cascade_id is None
    assert (
        RunEvent.model_validate(
            {
                "eventType": "COMPLETE",
                "eventTime": "2026-09-10T06:00:00Z",
                "producer": "external",
                "run": {"runId": "33333333-3333-5333-8333-333333333333", "facets": {}},
                "job": {"namespace": "other", "name": "j"},
            }
        ).cascade_id
        is None
    )


def test_a_non_string_batch_id_is_refused() -> None:
    """Producer-supplied. A number that merely parses must not become a batch id — the id is compared
    for equality when joining, so a value of a different type silently joins to nothing."""
    assert _event(operation="x", cascade_id=7).cascade_id is None
    assert _event(operation="x", cascade_id=True).cascade_id is None
    assert _event(operation="x", cascade_id="").cascade_id is None


def test_the_batch_id_is_STICKY_like_every_other_facet_field() -> None:
    """A reconcile or backfill event for the same graph run carries no lance facet. Clobbering the
    batch id to empty would detach a hop from its cascade — and it would do it to the hops that were
    reconciled, i.e. exactly the runs someone is investigating."""
    from lineage.services.cypher import MERGE_RUN

    assert "r.cascade_id=(CASE WHEN $cid = '' THEN r.cascade_id ELSE $cid END)" in MERGE_RUN


def test_BOTH_readers_declare_the_same_column_count_as_the_projection() -> None:
    """`RUN_BY_ID` is built from `LIST_RUNS`' body, so a column added to the board arrives here too —
    and each caller declares its own count. A mismatch is a 500 on every read, not a short row.

    MEASURED THE HARD WAY 2026-09-10: adding `cascade_id` widened both queries, only the board's caller
    was updated, and `GET /runs/{id}` answered 500 for every run. The board test passed the whole time
    because the board's own count was right — one projection, two counts, and only one of them checked.
    """
    import re
    from pathlib import Path

    from lineage.services import cypher as cy

    source = Path(__file__).resolve().parents[1].joinpath("src/lineage/services/repository.py").read_text(encoding="utf-8")
    declared = {int(n) for n in re.findall(r"cy\.(?:LIST_RUNS|RUN_BY_ID|list_runs_page\(limit\))[^)]*columns=(\d+)", source)}
    projected = cy.LIST_RUNS.count(",") + 1

    assert declared, "no caller of the run projection declares a column count — this gate is measuring nothing"
    assert declared == {projected}, f"the projection returns {projected} columns; callers declare {sorted(declared)} — a mismatch is a 500 per read"


def test_the_batch_id_comes_back_from_the_run_board() -> None:
    """Stored and unreadable is the state this file exists to end — `LIST_RUNS` is the projection both
    the board and the point read answer from."""
    from lineage.services.cypher import LIST_RUNS, RUN_BY_ID

    assert "r.cascade_id" in LIST_RUNS
    assert "r.cascade_id" in RUN_BY_ID
