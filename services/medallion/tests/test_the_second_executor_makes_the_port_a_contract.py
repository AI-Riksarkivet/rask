"""The IN-PROCESS engine conforms to the `Executor` port — docs/DECISIONS.md "The compute plane is decoupled" (§7.4) step 2.

That file calls this "the cheapest possible proof the contract is real, because the second engine
already exists", and the reason it matters is stated there too: **a port with one implementation is a
description, not a contract.** Everything `Executor` asserts about work would otherwise be an
account of what Ray happens to do.

What these hold is that a SYNCHRONOUS engine is expressible without contortion:

* `submit` runs to completion and returns a handle already terminal — the port does not assume a poll;
* `status` answers `UNKNOWN` for a handle this process never saw, which is the substantive change over
  today's `job_status`: it answers `None` for a 404 and every caller re-derives three distinct
  meanings by hand, either resubmitting live work or waiting on a job that is gone;
* `cancel` REFUSES, because this engine advertises no `CANCEL` capability. A port that made a
  silent no-op the only option would let a caller believe it had stopped something.
"""

from __future__ import annotations

from typing import Any

import pytest

from medallion.services.inprocess_executor import IN_PROCESS_ENGINE, InProcessExecutor, WrongEngineError
from service_kit.lakehouse.executor import Capability, Executor, RunHandle, RunState, SubmitOutcome, may_resubmit
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.work_order import WorkDestination, WorkIdentity, WorkOrder, WorkSource, WorkStamp


def _order(**over: Any) -> WorkOrder:
    base: dict[str, Any] = {
        "task": "stage-transform",
        "source": WorkSource(uri="mem://from", table_id="acme-bronze$events"),
        "destination": WorkDestination(uri="mem://to", table_id="acme-silver$features"),
        "stamp": WorkStamp(stage="silver", cardinality="1:1"),
        "identity": WorkIdentity(run_id="run-1", project="acme"),
        "idempotency_key": "silver:tok:bronze$events->silver$features",
    }
    return WorkOrder.model_validate(base | over)


def _registration(engine: str = IN_PROCESS_ENGINE) -> TaskRegistration:
    return TaskRegistration(task="stage-transform", engine=engine, command="medallion.transform_stage")


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch) -> InProcessExecutor:
    """The real adapter with only its WRITER stubbed — the port's behaviour is what is under test,
    and a fake executor would test the fake."""
    from medallion.services import inprocess_executor

    monkeypatch.setattr(inprocess_executor, "transform_stage", lambda *a, **k: {"version": 3})
    return InProcessExecutor(lambda: {})


def test_the_adapter_SATISFIES_the_port(executor: InProcessExecutor) -> None:
    """Asked at runtime rather than assumed. `Executor` is `runtime_checkable` for exactly this: a
    partial implementation is refused at the seam instead of raising later."""
    assert isinstance(executor, Executor)
    assert executor.name == IN_PROCESS_ENGINE


@pytest.mark.asyncio
async def test_a_SYNCHRONOUS_engine_returns_a_terminal_handle(executor: InProcessExecutor) -> None:
    """The port must not assume a poll. This engine's work is over before `submit` returns, so its
    first `status` is already terminal — and if that could not be expressed, `Executor` would be a
    Ray-shaped interface wearing a neutral name."""
    handle, outcome = await executor.submit(_order(), _registration())

    assert outcome is SubmitOutcome.SUBMITTED
    assert await executor.status(handle) is RunState.SUCCEEDED


@pytest.mark.asyncio
async def test_a_REDELIVERED_order_reattaches_rather_than_running_twice(executor: InProcessExecutor) -> None:
    """The order's `idempotency_key` is this engine's only durable identity, and it does the job a
    deterministic Ray submission id does: a second delivery of one order is one run."""
    first, _ = await executor.submit(_order(), _registration())
    _second, outcome = await executor.submit(_order(), _registration())

    assert outcome is SubmitOutcome.REATTACHED
    assert await executor.status(first) is RunState.SUCCEEDED


@pytest.mark.asyncio
async def test_an_UNKNOWN_handle_is_not_a_guess(executor: InProcessExecutor) -> None:
    """The substantive change over `job_status`, which answers `None` for a 404 and leaves every
    caller to disentangle not-yet-registered, record-lost and transport-blip by hand. A caller that
    gets it wrong either resubmits live work or waits on a job that is gone."""
    assert await executor.status(RunHandle(engine=IN_PROCESS_ENGINE, handle="never-seen")) is RunState.UNKNOWN


@pytest.mark.asyncio
async def test_a_FAILURE_is_classified_and_keeps_the_engine_s_own_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """`kind` is what a caller branches on; a free-text log line is not a classification. The MESSAGE
    stays verbatim because it becomes the run's FAIL `errorMessage`, which is what an operator reads
    to diagnose — a prefix would push the cause behind a label a machine reads and a person does not."""
    from medallion.services import inprocess_executor

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("lance write failed")

    monkeypatch.setattr(inprocess_executor, "transform_stage", _boom)
    executor = InProcessExecutor(lambda: {})

    handle, _ = await executor.submit(_order(), _registration())

    assert await executor.status(handle) is RunState.FAILED
    detail = await executor.failure(handle)
    assert detail is not None and detail.kind == "driver_error"
    assert detail.message == "lance write failed", "the engine's own message must survive to the FAIL event"


@pytest.mark.asyncio
async def test_CANCEL_refuses_because_the_capability_is_not_advertised(executor: InProcessExecutor) -> None:
    """A silent no-op would let a caller believe it had stopped something. The work is over before
    `submit` returns; refusing is the honest answer."""
    assert Capability.CANCEL not in executor.capabilities
    handle, _ = await executor.submit(_order(), _registration())

    with pytest.raises(NotImplementedError, match="CANCEL"):
        await executor.cancel(handle)


@pytest.mark.asyncio
async def test_a_task_for_ANOTHER_engine_is_refused_at_SUBMIT(executor: InProcessExecutor) -> None:
    """`validate_task` exists to refuse at DECLARATION time, and submit asks it again — a registration
    can be changed after a transform was declared against it, and running another plane's task here
    is how the wrong program rewrites a tenant's data while every status says success."""
    with pytest.raises(WrongEngineError, match="ray"):
        await executor.submit(_order(), _registration(engine="ray"))


def test_the_RESUBMIT_rule_reads_the_capability_not_a_habit() -> None:
    """`may_resubmit` is a property of the port rather than a rule each caller remembers.

    Against this engine — no `DURABLE_RECORD`, because its record dies with the process — an UNKNOWN
    handle may be resubmitted, since a lost record means the work was lost with it. Against an engine
    that DID promise durability the same machinery would be a spurious double-submit.
    """
    assert Capability.DURABLE_RECORD not in InProcessExecutor(lambda: {}).capabilities
    assert may_resubmit(RunState.UNKNOWN, capabilities=frozenset())
    assert not may_resubmit(RunState.UNKNOWN, capabilities=frozenset({Capability.DURABLE_RECORD}))
    assert not may_resubmit(RunState.RUNNING, capabilities=frozenset()), "resubmitting a live run puts two writers on one destination"
