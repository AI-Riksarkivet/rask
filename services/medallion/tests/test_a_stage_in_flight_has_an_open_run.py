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


def test_the_START_is_a_START_on_the_wire() -> None:
    assert _event("START")["eventType"] == "START"


def test_a_START_claims_no_version_it_has_not_written() -> None:
    """It is emitted BEFORE the job runs. A version or row count here would describe a write that has
    not happened — the same class of lie `transform.py:841-847` records the dispatch branch existing
    to prevent, where a measure ran before the job wrote and emitted a COMPLETE for a prior run."""
    started = _event("START", row_count=None)
    facets = started["outputs"][0].get("outputFacets") or {}

    assert "outputStatistics" not in facets, f"a START carries write statistics it cannot have: {facets}"


def test_whatever_runs_the_stage_opens_the_run_FIRST() -> None:
    """The behaviour the row is about: nothing may reach the write without a run already open.

    Keyed on the function that calls `_write_stage` rather than on a function NAME. The row cited
    `handle_stage`; the dispatch actually lives in `_run_compute`, and a gate naming the wrong frame
    would pass forever while the defect sat one call deeper. ORDER is asserted too — a START emitted
    after the write would open a run the terminal has already closed.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "src" / "medallion" / "services" / "transform.py").read_text())

    def _calls(node: ast.AST, name: str) -> list[ast.Call]:
        return [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == name]

    writers = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and _calls(n, "_write_stage")]
    assert writers, "nothing calls `_write_stage` any more — this gate is measuring a function that moved"

    for fn in writers:
        opens = _calls(fn, "_emit_start_run")
        assert opens, f"{fn.name} runs the stage without opening a run, so the job's whole runtime is invisible in the graph"
        assert min(c.lineno for c in opens) < min(c.lineno for c in _calls(fn, "_write_stage")), (
            f"{fn.name} opens the run AFTER the write, so the START lands on a run its terminal already closed"
        )


def test_opening_the_run_cannot_FAIL_the_run() -> None:
    """A START that raises would abort a stage before it wrote anything.

    Found by breaking it: the first version awaited the publish bare, and
    `tests/unit/test_outbox_complete_survives_fail.py` — which fails EVERY publish — went red with a
    FAIL staged where the COMPLETE belonged. The START had raised, the stage never ran, and the
    handler dutifully recorded a failure for work that was never attempted. A provenance hiccup had
    become a cascade failure.

    Every other emit in this module may propagate, and should: a terminal describes work that already
    happened, so losing it loses the only record. This one runs BEFORE the work, so it gets the
    opposite rule. The staged copy still carries the durability — `emit_lineage` stages before it
    publishes, so a swallowed publish leaves an object for the relay to drain.

    The emit is `emit_lineage`, the service's one signing door ([[LH-064]]), so the name this walks for
    is that call rather than the outbox seam it wraps. A gate that walks for a name nothing calls any
    more measures nothing, which is what this one said about itself when the seam moved.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "src" / "medallion" / "services" / "transform.py").read_text())
    emitter = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_emit_start_run")
    publishes = [c for c in ast.walk(emitter) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "emit_lineage"]

    assert publishes, "_emit_start_run no longer emits — this gate is measuring something that moved"
    guarded = [t for t in ast.walk(emitter) if isinstance(t, ast.Try) and any(p in ast.walk(t) for p in publishes)]
    assert guarded, "the START publish is unguarded, so a sidecar hiccup aborts a stage that has written nothing"
