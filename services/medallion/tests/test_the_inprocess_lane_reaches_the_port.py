"""The IN-PROCESS lane reaches the port, and reads a result only from an engine that promises one.

[[LH-158]], decided by the owner 2026-09-17. `docs/DECISIONS.md` describes "a port, TWO adapters" and
until this landed NEITHER lane went through it: `transform.py` hand-built
`InProcessExecutor(settings.storage_options)` at its one call site and `engine_registry.executor_for`
had ZERO production callers. The row calls keeping an uncalled port "the worst of the three" options,
because the decision record then documents an architecture nothing implements.

**THE EXISTING TESTS COULD NOT CATCH THIS, which is why this file exists rather than an assertion added
to one of them.** `test_the_chosen_engine_is_the_engine_that_runs` and `test_the_ray_lane_reaches_the_port`
both call `executor_for` FROM THE TEST and assert what it returns — so they prove the registry resolves,
and say nothing about whether anything in production asks it to. A registry with a green test and no
caller is exactly the shape they leave open.

WHY THE LANE BYPASSED THE PORT WAS A DESIGN TENSION, NOT AN OVERSIGHT. It calls `executor.result(handle)`
to avoid re-measuring a dataset `transform_stage` has already measured, and `result()` was BEYOND the
port — so resolving through `executor_for` would have returned an `Executor` that, as far as the
contract said, could not answer. The resolution is the pattern the port already uses twice: a
`Capability` member plus a method gated on it, exactly how `CANCEL` and `FAILURE_DETAIL` work. An
engine that cannot hand back an in-process measurement does not claim `RESULT`, and the caller
re-derives with `measure_stage` — which is what the Ray lane has always done.

SO THE ASYMMETRY IS NAMED RATHER THAN HIDDEN, and it was already real: `RayJobsApiExecutor` writes
out-of-process and has no measurement to return. Before this, that fact lived in a comment at the one
call site; now it is a property of the port that any third engine inherits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lineage_kit.consume import DatasetRef, LineageDoc
from lineage_kit.schemas import JobRef
from medallion.core.config import MedallionSettings
from medallion.services import transform
from medallion.services.compute import WriteResult
from medallion.services.engine_names import IN_PROCESS_ENGINE
from service_kit.lakehouse.executor import Capability, RunHandle, RunState, SubmitOutcome
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.work_order import WorkOrder


class _FakeExecutor:
    """An engine whose only interesting property is what it PROMISES.

    Deliberately not a `MagicMock`: the question these tests ask is what the lane does with a
    `capabilities` frozenset, and a mock answers every attribute truthily — including
    `Capability.RESULT in executor.capabilities`, which would make the gated branch look taken
    whatever the lane actually wrote.
    """

    name = "fake"

    def __init__(self, *, capabilities: frozenset[Capability], result_value: object = None) -> None:
        self.capabilities = capabilities
        self._result_value = result_value
        self.result_calls = 0

    def validate_task(self, registration: TaskRegistration) -> None:
        return None

    async def submit(self, order: WorkOrder, registration: TaskRegistration) -> tuple[RunHandle, SubmitOutcome]:
        return RunHandle(engine=self.name, handle=order.idempotency_key), SubmitOutcome.SUBMITTED

    async def status(self, handle: RunHandle) -> RunState:
        return RunState.SUCCEEDED

    async def failure(self, handle: RunHandle) -> None:
        return None

    async def cancel(self, handle: RunHandle) -> None:
        raise NotImplementedError("the fake engine advertises no CANCEL")

    async def result(self, handle: RunHandle) -> Any:  # noqa: ANN401 — the port's own return shape; see executor.py
        self.result_calls += 1
        if Capability.RESULT not in self.capabilities:
            raise NotImplementedError("the fake engine advertises no RESULT")
        return self._result_value


def _settings(tmp_path: Path) -> MedallionSettings:
    return MedallionSettings.model_validate({"control_root": str(tmp_path), "ray_code_version": "main-abc1234"})


def _lineage_doc() -> LineageDoc:
    return LineageDoc(
        run_id="9f1d0d2e-0000-4000-8000-000000000001",
        job=JobRef(namespace="medallion", name="silver"),
        event_time="2026-09-17T00:00:00Z",
        event_type="COMPLETE",
        producer="https://example.invalid/rask",
        output=DatasetRef(namespace="acme-silver", name="events"),
    )


async def _drive(settings: MedallionSettings) -> WriteResult:
    """The lane, called exactly as `_write_stage` calls it."""
    return await transform._run_in_process(
        settings,
        identity=transform.StageIdentity(
            from_namespace="acme-bronze", from_dataset="acme-bronze$events", to_namespace="acme-silver", to_dataset="acme-silver$events"
        ),
        from_uri="s3://lakehouse/bronze/events.lance",
        to_uri="s3://lakehouse/silver/events.lance",
        lineage_doc=_lineage_doc(),
        token=None,
        declared=None,
        project="acme",
    )


#: What a run measured. The values are arbitrary; what matters is WHICH path produced it.
_FROM_THE_ENGINE = WriteResult(version=7, row_count=11, size_bytes=222)
_RE_DERIVED = WriteResult(version=7, row_count=11, size_bytes=222, previous_row_count=3)


@pytest.mark.anyio
async def test_the_lane_resolves_its_engine_THROUGH_THE_REGISTRY(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEFECT ITSELF. The lane must ASK the registry for the engine rather than naming a class.

    Asserted by substituting the registry's answer and checking the lane used it — a source-text
    assertion would pass against an import that is never called, which is the state this row describes.
    """
    asked: list[str] = []
    fake = _FakeExecutor(capabilities=frozenset({Capability.RESULT}), result_value=_FROM_THE_ENGINE)

    def _executor_for(engine: str, **_kwargs: object) -> _FakeExecutor:
        asked.append(engine)
        return fake

    monkeypatch.setattr(transform, "executor_for", _executor_for)

    assert await _drive(_settings(tmp_path)) is _FROM_THE_ENGINE
    assert asked == [IN_PROCESS_ENGINE], f"the lane did not resolve through the registry; it asked for {asked}"


@pytest.mark.anyio
async def test_an_engine_that_promises_a_RESULT_is_not_measured_a_second_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The optimisation the hand-built adapter existed to keep, now expressed as a capability.

    `transform_stage` already calls `measure(to_uri)` internally, so re-deriving here would be a second
    stats read plus an upstream open for numbers identical by construction. That reasoning is unchanged
    — it is only now conditional on what the engine promises rather than on which class was constructed.
    """
    fake = _FakeExecutor(capabilities=frozenset({Capability.RESULT}), result_value=_FROM_THE_ENGINE)
    monkeypatch.setattr(transform, "executor_for", lambda *_a, **_k: fake)
    monkeypatch.setattr(transform, "measure_stage", _refuse_to_measure)

    assert await _drive(_settings(tmp_path)) is _FROM_THE_ENGINE
    assert fake.result_calls == 1


@pytest.mark.anyio
async def test_an_engine_that_promises_NO_result_makes_the_lane_RE_DERIVE(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE REASON THIS IS A CAPABILITY AND NOT A METHOD EVERY ADAPTER MUST IMPLEMENT.

    An engine that writes out-of-process has no measurement to hand back. The lane must then do what
    the Ray lane has always done — measure the destination — rather than raise, because the platform's
    contract is to RE-DERIVE what was written and `result()` is only ever an optimisation on top of it.
    A lane that raised here would make the port usable by exactly one engine.
    """
    fake = _FakeExecutor(capabilities=frozenset())
    monkeypatch.setattr(transform, "executor_for", lambda *_a, **_k: fake)
    monkeypatch.setattr(transform, "measure_stage", lambda *_a, **_k: _RE_DERIVED)

    assert await _drive(_settings(tmp_path)) is _RE_DERIVED
    assert fake.result_calls == 0, "the lane called a method the engine never promised"


def test_the_two_SHIPPED_adapters_disagree_about_RESULT(tmp_path: Path) -> None:
    """The asymmetry is real rather than hypothetical, and the declining adapter REFUSES.

    `InProcessExecutor` holds the Lance handle and measures as it writes; `RayJobsApiExecutor` submits
    to a cluster and never sees the table. A `result()` returning `None` on the second would be
    indistinguishable from a run that measured nothing, which is the overloaded-`None` defect the port's
    own `UNKNOWN` docstring exists to name — so it raises, exactly as `InProcessExecutor.cancel` does
    for the capability IT declines.
    """
    from medallion.services.engine_names import RAY_ENGINE
    from medallion.services.engine_registry import executor_for

    in_process = executor_for(IN_PROCESS_ENGINE, storage_options={})
    ray = executor_for(RAY_ENGINE, storage_options={})

    assert Capability.RESULT in in_process.capabilities
    assert Capability.RESULT not in ray.capabilities


def _refuse_to_measure(*_args: object, **_kwargs: object) -> WriteResult:
    raise AssertionError("the engine promised a RESULT and the lane measured the destination anyway")
