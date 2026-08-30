"""§2.13's pointer redesign left the PARENT counting a field it stopped emitting.

`enumerate_chunks` builds POINTER descriptors — `offset` + `count` into the run's unit manifest —
and leaves `keys` at its empty default (`workflow.py:878-879`, and the field's own docstring at :230
says so outright: "New descriptors leave these empty").

`ingest_run` then computed:

    units_total = sum(len(chunk.get("keys") or ()) for chunk in chunks)

which is ZERO for every chunk a current build produces. And zero is not inert — it is the
early-return condition:

    if units_total == 0:
        empty = RunOutcome(status="COMPLETE", rows=0)

So every real ingest run returned COMPLETE having published nothing, drained nothing and committed
nothing, BEFORE the fan-out. It also silently disabled the `max_units` ceiling, since 0 is never over
any bound — making the policy gate `resolve_limits` exists to enforce unreachable.

WHY NOTHING CAUGHT IT. Every existing test drives the workflow with the LEGACY inline shape
(`test_run_error_boundary.py:96` sends `[{"keys": ["a", "b"]}]`), so the descriptor the production
enumerator actually returns was never fed to the body. Green by absence, exactly like the NATS-gated
suites this session already unpicked.

`ChunkSpec.expected_units` (:240) is the accessor that resolves the pointer/inline split, and the
CHILD workflow already used it (:663). Only the parent did not.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from ingest.workflow import CHUNK_SIZE, ChunkSpec, ingest_run


class _Task:
    def __init__(self, result: Any = None) -> None:
        self.result = result


class _Ctx:
    """Records what the body yields and what status it publishes."""

    def __init__(self) -> None:
        self.actions: list[str] = []
        self.statuses: list[str] = []
        self.instance_id = "units-total-test"
        self.is_replaying = False

    def call_activity(self, activity: Any, *, input: Any = None, retry_policy: Any = None) -> _Task:  # noqa: A002
        self.actions.append(getattr(activity, "__name__", str(activity)))
        return _Task()

    def call_child_workflow(self, workflow: Any, *, input: Any = None, instance_id: str | None = None) -> _Task:  # noqa: A002
        self.actions.append("child:" + getattr(workflow, "__name__", str(workflow)))
        return _Task()

    def create_timer(self, _delta: Any) -> _Task:
        return _Task()

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)


SPEC = {"run_id": "pointer-test", "kind": "s3-prefix", "project": "bind86", "dataset": "pages", "options": {}}
NO_LIMITS = {"max_run_hours": 0.0, "max_units": 0}
HANDLE = {"location": "s3://wh/loc", "read_version": 7}


@pytest.fixture(autouse=True)
def _plain_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest import workflow as wf_module

    monkeypatch.setattr(wf_module.wf, "when_all", lambda tasks: _Task())
    monkeypatch.setattr(wf_module.wf, "when_any", lambda tasks: _Task())


def _real_descriptors(units: int) -> list[dict[str, Any]]:
    """Exactly what `enumerate_chunks` returns — pointers, no `keys`."""
    n = (units + CHUNK_SIZE - 1) // CHUNK_SIZE
    return [
        ChunkSpec(
            run_id="pointer-test",
            chunk_id=f"pointer-test-c{i}",
            offset=i * CHUNK_SIZE,
            count=min(CHUNK_SIZE, units - i * CHUNK_SIZE),
            dataset_uri="s3://wh/loc",
        ).model_dump()
        for i in range(n)
    ]


def test_a_POINTER_enumeration_is_counted_and_the_run_FANS_OUT() -> None:
    """THE defect. With pointer descriptors the run must dispatch, not report an empty COMPLETE."""
    ctx = _Ctx()
    gen = ingest_run(cast("Any", ctx), SPEC)
    gen.send(None)
    gen.send(None)
    gen.send(NO_LIMITS)
    gen.send(HANDLE)
    gen.send(_real_descriptors(2_500))  # 3 chunks: 1000 + 1000 + 500

    status = json.loads(ctx.statuses[-1])
    assert status["units_total"] == 2_500, (
        f"the parent counted {status['units_total']} units for a 2,500-unit enumeration — it is summing "
        f"`keys`, which pointer descriptors leave empty, so the run reports COMPLETE having done nothing"
    )
    assert any(a.startswith("child:") for a in ctx.actions), "the run never fanned out — it took the empty-run early return"


def test_the_LEGACY_inline_shape_still_counts() -> None:
    """Backward compatibility is the reason `expected_units` exists rather than a bare `count` read:
    a descriptor enumerated before the pointer change still carries its keys, and a run in flight
    across that deploy must not become an empty COMPLETE either."""
    ctx = _Ctx()
    gen = ingest_run(cast("Any", ctx), SPEC)
    gen.send(None)
    gen.send(None)
    gen.send(NO_LIMITS)
    gen.send(HANDLE)
    gen.send([{"run_id": "pointer-test", "chunk_id": "c0", "keys": ["a", "b", "c"]}])

    assert json.loads(ctx.statuses[-1])["units_total"] == 3


def test_a_GENUINELY_empty_enumeration_still_short_circuits() -> None:
    """The early return is correct and must survive the fix — a source with nothing to ingest should
    not open the dataset or dispatch a child."""
    ctx = _Ctx()
    gen = ingest_run(cast("Any", ctx), SPEC)
    gen.send(None)
    gen.send(None)
    gen.send(NO_LIMITS)
    gen.send(HANDLE)
    gen.send([])  # empty enumeration -> the body yields its terminal emit, then returns
    with pytest.raises(StopIteration) as stop:
        gen.send(None)

    assert stop.value.value["status"] == "COMPLETE"
    assert stop.value.value["units_total"] == 0
    assert not any(a.startswith("child:") for a in ctx.actions), "an empty run must not dispatch a child"


def test_an_ENUMERATION_REFUSAL_renders_as_FAILED_not_an_AttributeError() -> None:
    """The refusal branch was UNREACHABLE, and it is the ceiling guard's only rendering path.

    `enumerate_chunks` may return a compact dict `{"__refused__": reason}` instead of a chunk list —
    that is how the unit ceiling and the dispatch budget refuse, "both decided where the payload is
    built because neither can be decided after it has failed to arrive" (:449).

    But `units_total = sum(... for chunk in chunks)` ran FIRST, and iterating a dict yields its string
    KEYS — so `chunk.get(...)` raised `AttributeError: 'str' object has no attribute 'get'` before the
    `isinstance(chunks, dict)` check twelve lines later could ever see it. A refusal therefore
    surfaced as an unhandled workflow crash rather than the FAILED outcome carrying its reason, and
    the ceiling `resolve_limits` exists to enforce could not report why it fired.

    Order matters here and nowhere else in this body: the refusal must be recognised BEFORE anything
    tries to treat the return value as a sequence of chunks.
    """
    ctx = _Ctx()
    gen = ingest_run(cast("Any", ctx), SPEC)
    gen.send(None)
    gen.send(None)
    gen.send(NO_LIMITS)
    gen.send(HANDLE)

    # The refusal shape, exactly as `_refuse_oversized_dispatch` builds it.
    gen.send({"__refused__": "2,000,000 units exceeds the 500,000 ceiling"})
    with pytest.raises(StopIteration) as stop:
        gen.send(None)

    outcome = stop.value.value
    assert outcome["status"] == "FAILED", "a refused enumeration must render as FAILED, not crash the workflow"
    assert "ceiling" in json.dumps(outcome), "the refusal must carry its REASON to the operator"
    assert not any(a.startswith("child:") for a in ctx.actions), "a refused run must not dispatch"
