"""The fan-out's progress signal existed and nothing read it.

`chunk_run` publishes per-chunk progress on the CHILD instance under a heading calling it "THE
FAN-OUT'S ONLY PROGRESS SIGNAL, and it has to come from the child", and `runs.py` promises "a run
says '320 of 500' while it is still going". But `_DaprWorkflowReader.state` fetched the PARENT
instance only, and the parent's custom status during the fan-out is what it set BEFORE dispatch --
`{"units_total": N, "chunks": M}`, with no `units_done` key at all. `units_done` first appears after
`when_all` has already returned.

So for the whole multi-hour fan-out the status endpoint fell through `output["rows"]` (absent until
finalize), then the parent's `units_done` (absent), to `record.units_done`, which the accept path set
to 0 and nothing ever writes. An operator watching a 10M-unit harvest read "0 of 10,000,000" for
hours on a run that was landing rows the entire time -- indistinguishable from a wedged run, which is
the state this plane's terminate door exists for.

FIXED ON THE READ SIDE, deliberately. The alternative -- aggregating in the parent by racing a timer
against the fan-out -- adds timers to `ingest_run`'s action stream, which breaks replay for every
in-flight instance and would need a drain. Summing the children costs nothing durable: the child ids
are derived exactly as the workflow derives them (`{run_id}-c{i}`), and the chunk count is already in
the parent's own custom status.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


class _State:
    def __init__(self, custom: dict[str, Any] | None, status: str = "RUNNING", output: str = "") -> None:
        self._custom = custom
        self._status = status
        self._output = output

    def to_json(self) -> dict[str, Any]:
        return {
            "runtime_status": self._status,
            "serialized_custom_status": json.dumps(self._custom) if self._custom is not None else None,
            "serialized_output": self._output,
        }


class _Client:
    """A workflow client that knows the parent AND its children, like the real engine does."""

    def __init__(self, instances: dict[str, _State]) -> None:
        self._instances = instances
        self.asked: list[str] = []

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _State | None:
        self.asked.append(instance_id)
        return self._instances.get(instance_id)


RUN = "run-1"


def _install(monkeypatch: pytest.MonkeyPatch, instances: dict[str, _State]) -> _Client:
    """Patch the SDK's client factory on the real module.

    Not a `sys.modules` injection: `_DaprWorkflowReader.state` does `import dapr.ext.workflow as wf`,
    which needs the real `dapr.ext` package to resolve before the `as` binding is even attempted — an
    injected submodule leaves the import raising `cannot import name 'ext' from 'dapr'`, the reader
    swallowing it, and every assertion below passing or failing for the wrong reason.
    """
    import dapr.ext.workflow as wf

    client = _Client(instances)
    monkeypatch.setattr(wf, "DaprWorkflowClient", lambda: client)
    return client


def _reader() -> Any:
    from ingest import _DaprWorkflowReader

    return _DaprWorkflowReader()


def test_the_fan_outs_progress_REACHES_the_status_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE WEDGE. Mid-fan-out the parent has no `units_done`; the children have all of it."""
    client = _install(
        monkeypatch,
        {
            RUN: _State({"units_total": 500, "chunks": 3}),
            f"{RUN}-c0": _State({"chunk_id": "c0", "units_done": 200, "units_expected": 200}),
            f"{RUN}-c1": _State({"chunk_id": "c1", "units_done": 120, "units_expected": 200}),
            f"{RUN}-c2": _State({"chunk_id": "c2", "units_done": 0, "units_expected": 100}),
        },
    )

    state = _reader().state(RUN)

    assert state is not None
    custom = json.loads(state["serialized_custom_status"] or "{}")
    assert custom.get("units_done") == 320, f"the children's progress never reached the read; got {custom}"
    assert custom.get("units_total") == 500, "the denominator must survive the aggregation"
    assert client.asked[0] == RUN, "the parent must still be read first — the chunk count comes from it"


def test_a_child_the_engine_does_not_know_yet_is_simply_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Children are scheduled as a batch but appear one at a time. A missing child is 'not started',
    not an error, and must not blank the whole progress read."""
    _install(
        monkeypatch,
        {
            RUN: _State({"units_total": 500, "chunks": 3}),
            f"{RUN}-c0": _State({"chunk_id": "c0", "units_done": 200, "units_expected": 200}),
        },
    )

    custom = json.loads((_reader().state(RUN) or {})["serialized_custom_status"] or "{}")

    assert custom.get("units_done") == 200


def test_a_parent_with_NO_chunk_count_is_not_fanned_out_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Before `enumerate_chunks` returns there are no children, and guessing an id range would cost a
    round-trip per guess against an engine that will answer None to every one."""
    client = _install(monkeypatch, {RUN: _State({"units_total": 500})})

    _reader().state(RUN)

    assert client.asked == [RUN], f"the reader fanned out with no chunk count: {client.asked}"


def test_a_TERMINAL_run_does_not_pay_for_a_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once `finalize` has returned, the output carries the real total and the children are history.
    Asking for them would be N wasted round-trips on every poll of a finished run."""
    client = _install(
        monkeypatch,
        {
            RUN: _State({"units_total": 500, "chunks": 3, "units_done": 500}, status="COMPLETED", output=json.dumps({"rows": 500})),
            f"{RUN}-c0": _State({"chunk_id": "c0", "units_done": 200, "units_expected": 200}),
        },
    )

    _reader().state(RUN)

    assert client.asked == [RUN], f"a completed run fanned out anyway: {client.asked}"


def test_an_UNREACHABLE_engine_still_returns_None_rather_than_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-existing contract: a status endpoint that 500s when the engine is unreachable fails at
    precisely the moment an operator is using it to find out why."""
    import dapr.ext.workflow as wf

    def _boom() -> Any:
        raise RuntimeError("no sidecar")

    monkeypatch.setattr(wf, "DaprWorkflowClient", _boom)

    assert _reader().state(RUN) is None


def test_a_child_read_that_RAISES_abandons_the_sum_rather_than_undercounting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial sum renders as progress going BACKWARDS on the next poll, which reads as corruption
    rather than as a failed read. Falling back to the parent's own status is the honest answer."""
    import dapr.ext.workflow as wf

    class _Flaky(_Client):
        def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _State | None:
            if instance_id.endswith("-c1"):
                raise RuntimeError("sidecar hiccup")
            return super().get_workflow_state(instance_id, fetch_payloads=fetch_payloads)

    client = _Flaky(
        {
            RUN: _State({"units_total": 500, "chunks": 3}),
            f"{RUN}-c0": _State({"chunk_id": "c0", "units_done": 200, "units_expected": 200}),
        }
    )
    monkeypatch.setattr(wf, "DaprWorkflowClient", lambda: client)

    custom = json.loads((_reader().state(RUN) or {})["serialized_custom_status"] or "{}")

    assert "units_done" not in custom, f"a failed child read produced an undercount: {custom}"
