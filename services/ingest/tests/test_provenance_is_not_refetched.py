"""ING-14 — a completed run's A8 verdict is resolved once, not on every status read.

`GET /ingests/{run_id}` joins the run against the lineage graph for every COMPLETE /
COMPLETE_WITH_ERRORS read, and that join is `GET /runs` — the estate's whole run board, with no
run-id filter and no pagination (`services/lineage/.../runs.py:54` takes none) — followed by a linear
scan. The compute zone polls that status endpoint, so a finished run re-downloaded the entire board
every couple of seconds, forever.

A run that IS in the graph is in it permanently, so the answer is memoizable. These pin the fetch
COUNT, because the verdict is identical either way.
"""

from __future__ import annotations

import httpx
import pytest

from ingest.lineage import lineage_run_id
from ingest.provenance import LineageProvenanceReader


def _graph(monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, str]], *, status_code: int = 200) -> list[str]:
    """Serve lineage's per-run point read, recording every fetch.

    Answers the run when the URL names one this graph holds, and 404 otherwise — which is the real
    endpoint's contract, and the one the reader must read as "absent" rather than as an outage.
    """
    present = {run["run_id"] for run in runs}
    fetched: list[str] = []

    def _get(_client: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        fetched.append(url)
        asked = url.rstrip("/").rsplit("/", 1)[-1]
        if status_code != 200 or asked not in present:
            return httpx.Response(404 if status_code == 200 else status_code, json={"detail": "run not found"}, request=httpx.Request("GET", url))
        return httpx.Response(200, json={"run_id": asked}, request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.Client.get", _get)
    return fetched


def test_the_check_asks_for_ONE_RUN_not_for_the_whole_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """ING-14's other half, and a REGRESSION the board's own bound would otherwise have caused.

    Downloading every run to answer "is THIS run there?" was always the wrong shape, and it became
    wrong in a second way the moment `/runs` was bounded (2026-09-07): the board answers the 200
    newest runs, so a run older than those is simply not in the response and the linear scan reports
    it ABSENT. That is not a slow answer, it is a WRONG one — A8 would report a provenance defect on
    a run whose lineage is in the graph, silently, and only for older runs.

    So the question is asked of the run: a point read on `/runs/{id}`, which the graph answers from
    `MATCH (r:Run {run_id: $rid})` rather than by returning the estate.
    """
    fetched = _graph(monkeypatch, [{"run_id": lineage_run_id("run-42")}])

    assert LineageProvenanceReader().has_run("run-42") is True
    assert fetched, "the reader asked lineage nothing at all"
    asked = fetched[0]
    assert asked.rstrip("/").endswith(lineage_run_id("run-42")), (
        f"the reader fetched {asked!r} — it must ask for the ONE run, or a bounded board reports an older run as absent"
    )


def test_a_present_run_is_asked_for_ONCE(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = _graph(monkeypatch, [{"run_id": lineage_run_id("run-42")}])
    reader = LineageProvenanceReader()

    assert reader.has_run("run-42") is True
    assert reader.has_run("run-42") is True
    assert reader.has_run("run-42") is True

    assert len(fetched) == 1, f"lineage was asked {len(fetched)} times about one settled run"


def test_two_DIFFERENT_runs_still_get_their_own_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The memo must key on the run, not merely suppress the second call."""
    fetched = _graph(monkeypatch, [{"run_id": lineage_run_id("run-a")}])
    reader = LineageProvenanceReader()

    assert reader.has_run("run-a") is True
    assert reader.has_run("run-b") is False
    assert len(fetched) == 2


def test_an_ABSENT_run_is_re_asked_because_its_lineage_may_still_land(monkeypatch: pytest.MonkeyPatch) -> None:
    """A negative is a snapshot, not a verdict: the emit can arrive after the first poll."""
    fetched = _graph(monkeypatch, [])
    reader = LineageProvenanceReader()

    assert reader.has_run("run-42") is False
    assert reader.has_run("run-42") is False

    assert len(fetched) == 2, "an ABSENT answer was cached, so a run whose lineage lands later stays a defect forever"


def test_an_UNREACHABLE_graph_is_never_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "We could not ask" is not an answer, so it must not be remembered as one."""
    calls: list[str] = []

    def _explode(_client: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        calls.append(url)
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("httpx.Client.get", _explode)
    reader = LineageProvenanceReader()

    assert reader.has_run("run-42") is None
    assert reader.has_run("run-42") is None
    assert len(calls) == 2


def test_the_memo_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ingest pod serves runs indefinitely; an unbounded memo is the leak `lineage.py` already fixed."""
    from ingest.provenance import MEMO_MAX_RUNS

    _graph(monkeypatch, [{"run_id": lineage_run_id(f"run-{i}")} for i in range(MEMO_MAX_RUNS + 10)])
    reader = LineageProvenanceReader()
    for i in range(MEMO_MAX_RUNS + 10):
        assert reader.has_run(f"run-{i}") is True

    assert reader.memoized() <= MEMO_MAX_RUNS, f"the memo grew to {reader.memoized()} entries"
