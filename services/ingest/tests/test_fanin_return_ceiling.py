"""The RETURN leg respects the same gRPC ceiling the dispatch leg already does (DWF-ACT-004).

`enumerate_chunks` is guarded: `_refuse_oversized_dispatch` measures the payload against
`CHUNK_DISPATCH_BUDGET_BYTES` (3 MiB, under grpc's 4 MiB default) before building a message the
transport would reject. The other direction had no such guard. Every `chunk_run` returned the full
serialised `FragmentMetadata` JSON for its chunk, the parent flattened all of them, and the whole
list rode into `finalize` as one activity INPUT — so it was persisted in workflow history and
re-delivered to the worker on every parent replay.

WHAT THE CARRIED LIST ACTUALLY IS, because it decides the shape of the fix. `finalize_run` commits
what `discover_staged` finds -- "STORAGE TRUTH, and it is the ONLY truth", an exact-cover selection
that deliberately deselects a fragment another already covers. The carried list is reached only when
staging returns NOTHING while the workflow still holds fragments, i.e. the staging prefix was
unreadable or its manifests were truncated. Unioning the two caused a real duplication bug
(`test_partial_ack_duplication.py`, "four units in, six rows out"), so the carried list is a
fallback and nothing else.

That is why a staging-prefix POINTER cannot replace it: the pointer is worthless in precisely the
case the fallback exists for. Owner ruling (2026-08-25): bound the carried list and keep the
fallback for every run that fits. Past the budget the blobs are dropped and the run says so --
`fallback_dropped` rides into `finalize`, and `finalize_run` logs an ERROR rather than letting an
unreadable staging prefix look like an ordinary empty run.

Measured: ~395 bytes per fragment manifest at `fragment_rows=1024`, so the budget is reached around
8M rows -- inside the estate's stated scale of 10M images.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, cast

import pytest
from dapr.ext.workflow import DaprWorkflowContext

from ingest.workflow import CHUNK_DISPATCH_BUDGET_BYTES, FANIN_RETURN_BUDGET_BYTES, ingest_run


_FANOUT: list[Any] = []


def _remember_fanout(_tasks: Any) -> Any:
    """`when_all`'s result, kept so a test can hand the fan-in its chunk results.

    The workflow now reads them via `fanout.get_result()` rather than from the value sent into
    the yield, because the fan-in races cancellation and the sent value is the WINNER."""
    task = _Task()
    _FANOUT.append(task)
    return task


def _recorded(payload: object) -> dict[str, Any]:
    """An activity input as the RUNTIME records it: JSON, never the model instance.

    Activities declare Pydantic inputs now (DWF-ACT-009) and the SDK coerces on the worker side, so a
    workflow body hands `call_activity` a MODEL. History still holds the serialized form, so a fake
    context that stored the instance would let assertions read attributes the real recorded payload
    does not have — a double diverging from the thing it doubles.
    """
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    return payload if isinstance(payload, dict) else {}


class _Task:
    """A stand-in for a durabletask task — identity is all the `when_*` comparisons use."""

    def __init__(self) -> None:
        self.result: Any = None

    def get_result(self) -> Any:
        return self.result


def _finish_fanout(gen: Any, results: list[dict[str, Any]]) -> Any:
    """Complete the fan-out with `results`, then let the race resolve to it.

    The fan-in now races cancellation, so the value sent into the yield is the WINNER and the chunk
    results are read from `fanout.get_result()`. Sending them directly would make the workflow treat
    a list of chunk results as the winning task.
    """
    _FANOUT[-1].result = results
    return gen.send(_FANOUT[-1])


class _Ctx:
    """Records what the workflow asks the runtime for, in order."""

    def __init__(self) -> None:
        self.activities: list[tuple[str, dict[str, Any]]] = []
        self.timers: int = 0

    def call_activity(self, fn: Any, *, input: Any = None, retry_policy: Any = None) -> _Task:  # noqa: A002 — the runtime's own keyword
        self.activities.append((getattr(fn, "__name__", str(fn)), _recorded(input)))
        return _Task()

    def call_child_workflow(self, fn: Any, *, input: Any = None, instance_id: str | None = None) -> _Task:  # noqa: A002
        return _Task()

    def wait_for_external_event(self, _name: str) -> _Task:
        """The cancellation seam. `terminate` raises this instead of killing the instance, so the
        run reaches its own cleanup — a fake that omits it fails as an AttributeError swallowed by
        the error boundary, which reads as a product bug rather than a fixture gap."""
        return _Task()

    def create_timer(self, _delta: Any) -> _Task:
        self.timers += 1
        return _Task()

    def set_custom_status(self, status: str) -> None: ...


SPEC = {"run_id": "fanin-ceiling", "kind": "s3-prefix", "project": "bind86", "dataset": "pages", "options": {}}
HANDLE = {"location": "s3://wh/pages.lance", "read_version": 5}


# Inlined rather than imported from `test_replay_hygiene`: `--import-mode=importlib` gives test
# modules no implied package path, so a cross-module relative import does not resolve.
@pytest.fixture(autouse=True)
def _plain_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`when_all`/`when_any` wrap tasks in bookkeeping these stand-ins do not carry; the workflow
    only ever YIELDS the wrapper, so a bare task is a faithful double at this seam."""
    from ingest import workflow as wf_module

    monkeypatch.setattr(wf_module.wf, "when_all", _remember_fanout)
    monkeypatch.setattr(wf_module.wf, "when_any", lambda tasks: _Task())


#: A realistic serialised FragmentMetadata blob. Padded to the measured width rather than a token
#: string, because the whole question is how many of these fit in one gRPC message.
_FRAGMENT_BYTES = 395


def _fragment(index: int) -> str:
    body = f'{{"id":{index},"files":[{{"path":"data/{index:012d}.lance","fields":[0,1,2,3,4]}}],"physical_rows":1024,'
    return body + '"pad":"' + "x" * max(0, _FRAGMENT_BYTES - len(body) - 10) + '"}'


def _drive_to_finalize(ctx: _Ctx, results: list[dict[str, Any]]) -> None:
    """emit_start -> resolve_limits -> ensure_dataset -> enumerate_chunks -> fan-in -> finalize.

    `max_run_hours=0.0` so no deadline timer is created and the fan-in is a plain `yield fanout`,
    which is one send rather than a `when_any` race — the deadline branch has its own file.
    """
    gen = ingest_run(cast(DaprWorkflowContext, ctx), SPEC)
    gen.send(None)
    gen.send(None)
    gen.send({"max_run_hours": 0.0, "max_units": 0})
    gen.send(HANDLE)
    gen.send([{"keys": ["a", "b"]}])
    _finish_fanout(gen, results)


def _chunk_result(fragments: list[str]) -> dict[str, Any]:
    return {"chunk_id": "c0", "units_done": len(fragments), "fragments": fragments, "errors": {}, "errors_total": 0}


def test_the_fan_in_carries_the_fragments_while_they_fit() -> None:
    """The common case is unchanged: a run inside the budget keeps its fallback, exactly as before.

    This is the half the owner ruling protects. Bounding the list must not quietly remove the
    loss-avoiding path for the runs that never had a size problem.
    """
    ctx = _Ctx()
    fragments = [_fragment(i) for i in range(3)]

    _drive_to_finalize(ctx, [_chunk_result(fragments)])

    name, payload = ctx.activities[-1]
    assert name == "finalize"
    assert payload["fragments"] == fragments, "a run inside the budget must still carry its fallback"
    assert payload["fallback_dropped"] is False


def test_the_fan_in_DROPS_the_carried_fragments_past_the_budget_and_says_so() -> None:
    """The wedge. Unbounded, this list is persisted in history and re-delivered on every replay.

    Failing this test before the fix is the whole point: the parent flattened every child's blobs
    into `finalize`'s input with nothing measuring them, so a large enough run built a message grpc
    refuses — surfacing as RESOURCE_EXHAUSTED from inside the SDK, four ACTIVITY_RETRY attempts
    against a permanently-failing input, and a wedged workflow with nothing naming a knob.
    """
    ctx = _Ctx()
    over = (FANIN_RETURN_BUDGET_BYTES // _FRAGMENT_BYTES) + 50
    fragments = [_fragment(i) for i in range(over)]
    assert len(json.dumps(fragments)) > FANIN_RETURN_BUDGET_BYTES, "the fixture must actually exceed the budget"

    _drive_to_finalize(ctx, [_chunk_result(fragments)])

    name, payload = ctx.activities[-1]
    assert name == "finalize"
    assert payload["fragments"] == [], "the oversized carried list rode into workflow history"
    assert payload["fallback_dropped"] is True, "dropping the fallback silently is the failure this replaces"


def test_the_two_legs_are_measured_against_ONE_ceiling() -> None:
    """Both directions cross the sidecar as one gRPC message, so both answer to the same number.

    Two independently-tuned budgets would drift, and the one that drifted upward would wedge exactly
    the way the unguarded return leg did.
    """
    assert FANIN_RETURN_BUDGET_BYTES == CHUNK_DISPATCH_BUDGET_BYTES


def test_a_dropped_fallback_meeting_unreadable_staging_is_reported_not_silent(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The one case the bound was allowed to introduce, and the reason it may not stay quiet.

    Bounding the carried list trades a fallback away for runs past the budget. That is only
    acceptable while the trade is VISIBLE: if staging is then also unreadable, the run wrote rows and
    neither source can name them, and `finalize_run`'s ordinary "nothing to commit" no-op would
    present that as an empty run. An empty list means two different things now, so the flag has to
    separate them.
    """
    import logging

    from ingest import runtime as runtime_module
    from ingest.workflow import RunSpec

    # `finalize_run` imports `discover_staged` LOCALLY, so it must be patched at its source module —
    # patching the runtime module's namespace binds nothing the function will look at.
    monkeypatch.setattr(runtime_module, "_catalog", lambda: _StubCatalog())
    monkeypatch.setattr("ingest.staging.discover_staged", lambda *_a, **_k: [])

    spec = RunSpec.model_validate(SPEC)
    with caplog.at_level(logging.ERROR, logger="ingest.runtime"), contextlib.suppress(Exception):
        # The SUBJECT is the log line. What `finalize_run` does after it is the pre-existing
        # empty-commit path, which reaches a real object store — out of scope here and covered by
        # `test_empty_commit.py`. Suppressed rather than stubbed so this test cannot start silently
        # asserting something about the commit path it does not model.
        runtime_module.finalize_run(spec, [], {}, read_version=5, fallback_dropped=True)

    assert any(r.message == "ingest_staging_unreadable_and_fallback_dropped" for r in caplog.records), (
        f"a dropped fallback meeting unreadable staging was reported as an ordinary empty run; records were {[r.message for r in caplog.records]}"
    )


class _StubCatalog:
    """Just enough to reach the empty-commit branch without a store."""

    def ensure(self, _namespace: str, _dataset: str) -> str:
        return "s3://wh/pages.lance"
