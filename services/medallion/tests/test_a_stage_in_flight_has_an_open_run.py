"""A dispatched stage has an OPEN run in the lineage graph, not silence until its terminal.

[[LH-173]]. The medallion emits `FAIL` (`transform.py:304`, `workflow.py:761`) and the `COMPLETE`
default and nothing else — `grep -rn '"START"' services/ scripts/ runners/` finds the ingest plane and
`ray_train_job.py`, never the cascade. On the Ray lane that compounds: the dispatch branch returns
`None  # DISPATCHED` having emitted nothing, and the one event for the whole run is the terminal
published after the workflow's wake-up. Between those two moments — the entire runtime of the job —
NO RUN EXISTS IN THE GRAPH, so a hop that died before its terminal is indistinguishable there from one
that never began.

THE MERGE IS THE WHOLE MECHANISM, and it is why this is a small change rather than a new lane.
`run_id` is derived, not minted: `events.py:337` is `run_id_for("\\x00".join((project, operation,
token)))`, so a START and its terminal built from the same token carry the same `runId` and the
consumer's idempotent MERGE closes the run it opened. A START with a fresh id would create a second
node and make the graph worse, which is what the first assertion here exists to prevent.

THIS EMITS FOR THE GRAPH AND DELIBERATELY NOTIFIES NOBODY. `notifications/api/lineage_events.py:27`
records that START/RUNNING target no one by product decision, so nothing downstream should read this
as a delivery change — the row's original `notifications` tag was wrong and is dropped.
"""

from __future__ import annotations

from medallion.schemas.events import build_run_event


def _event(event_type: str, row_count: int | None = None) -> dict:
    """One stage event, with every argument NAMED rather than splatted.

    Neither a `**mapping` nor a `**kwargs` passthrough can be proven against `build_run_event`'s
    heterogeneous signature, and the diagnostics land on the test rather than on anything real — so
    the call is spelled out and the one thing a caller varies is its own parameter.
    """
    return build_run_event(
        operation="bronze-to-silver",
        author="data_eng",
        author_subject="service-bronze-to-silver",
        job_namespace="rask",
        inputs=[("acme-bronze", "acme-bronze$events")],
        output_namespace="acme-silver",
        output_name="acme-silver$events",
        token="tok-42",
        project="acme",
        event_type=event_type,
        row_count=row_count,
    )


def test_a_START_and_its_terminal_share_one_run_id() -> None:
    """Otherwise the START opens a run the terminal never closes, and the graph gains a node per hop."""
    started = _event("START")
    completed = _event("COMPLETE")

    assert started["run"]["runId"] == completed["run"]["runId"], (
        "a START would open a different run from the one its terminal closes, so every dispatched hop would leave an orphan open run behind it"
    )
