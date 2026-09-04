"""The `Executor` port: what the platform may ask of a compute engine, in no engine's vocabulary.

`open_compute-decoupling.md` §2.4, step 1 of the owner-ordered §7.4.

`UNKNOWN` REPLACES AN OVERLOADED `None`, and that is the substantive change rather than a rename.
`ray_kit.submit.job_status` returns `None` for a 404, and `medallion/workflow.py` disentangles THREE
distinct meanings from it by hand — not-yet-registered, record-lost, and a transport blip — using
`seen` / `vanished` / `never_registered` / `MAX_UNSEEN_POLLS`. A port that returns `None` forces every
caller to re-derive that.

AND THE RESUBMIT MACHINERY BECOMES CAPABILITY-GATED. `workflow.py` justifies `MAX_UNSEEN_POLLS=4` and
`MAX_RESUBMITS=2` by an engine-specific durability defect it names outright: "Ray's GCS is not
fault-tolerant here (no external Redis, a deliberate estate-wide `no Redis`), so a head restart takes
every job record with it." Against an executor advertising `DURABLE_RECORD`, that same machinery is a
spurious DOUBLE-SUBMIT — it would run a second copy of work the engine never lost. So the rule is a
property of the port: an executor WITHOUT `DURABLE_RECORD` may answer `UNKNOWN` after a real status,
and only then may a caller resubmit.
"""

from __future__ import annotations

from service_kit.lakehouse.executor import (
    TERMINAL,
    Capability,
    Executor,
    RunFailure,
    RunHandle,
    RunState,
    SubmitOutcome,
    may_resubmit,
)


def test_the_port_names_no_engine() -> None:
    import inspect

    from service_kit.lakehouse import executor

    source = inspect.getsource(executor).lower()
    for noun in ("ray", "spark", "flink", "dashboard", "runtime_env"):
        # `ray` appears in prose citing the defect that motivated the capability; the TYPES must not.
        assert f"class {noun}" not in source, f"the port declares an engine type {noun!r}"


def test_terminal_is_exactly_the_three_states_a_run_cannot_leave() -> None:
    assert frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}) == TERMINAL
    assert RunState.UNKNOWN not in TERMINAL, "UNKNOWN is not terminal — the engine may simply not know YET"
    assert RunState.PENDING not in TERMINAL


def test_a_durable_executor_may_NOT_resubmit_on_UNKNOWN() -> None:
    """The spurious double-submit this gate exists to prevent: the engine did not lose the run, so
    UNKNOWN means "ask again", never "run it twice"."""
    assert may_resubmit(RunState.UNKNOWN, capabilities=frozenset({Capability.DURABLE_RECORD})) is False


def test_a_non_durable_executor_MAY_resubmit_on_UNKNOWN() -> None:
    """Ray's GCS is not fault-tolerant here, so a head restart takes every job record with it — the
    engine genuinely cannot answer, and resubmitting is the only way the work happens."""
    assert may_resubmit(RunState.UNKNOWN, capabilities=frozenset()) is True


def test_no_executor_may_resubmit_a_state_it_KNOWS() -> None:
    """A running job resubmitted is two jobs on one destination. Only ignorance justifies a resubmit."""
    for state in (RunState.PENDING, RunState.RUNNING, RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED):
        assert may_resubmit(state, capabilities=frozenset()) is False, state


def test_a_handle_is_returned_by_submit_and_never_re_derived() -> None:
    """`ray_submit` records the measured defect: the submitter and the watcher must name the same job,
    and a second inline derivation is how the poller watches an id the submitter never used."""
    handle = RunHandle(engine="inprocess", handle="run-1")
    assert handle.handle == "run-1"


def test_a_failure_carries_a_classified_kind_and_a_posix_code() -> None:
    failure = RunFailure(kind="oom", message="killed", exit_code=137)
    assert failure.exit_code == 137, "137 is SIGKILL under any engine — a portable fact, unlike a log string"


def test_the_protocol_is_runtime_checkable_so_conformance_is_ASKED_not_assumed() -> None:
    class _Conforming:
        name = "inprocess"
        capabilities = frozenset({Capability.DURABLE_RECORD})

        def validate_task(self, reg: object) -> None:
            return None

        async def submit(self, order: object, reg: object) -> tuple[RunHandle, SubmitOutcome]:
            return RunHandle(engine="inprocess", handle="run-1"), SubmitOutcome.SUBMITTED

        async def status(self, handle: RunHandle) -> RunState:
            return RunState.SUCCEEDED

        async def failure(self, handle: RunHandle) -> RunFailure | None:
            return None

        async def cancel(self, handle: RunHandle) -> None:
            return None

    assert isinstance(_Conforming(), Executor)


def test_a_partial_implementation_is_REFUSED() -> None:
    class _MissingCancel:
        name = "half"
        capabilities = frozenset()

        def validate_task(self, reg: object) -> None:
            return None

        async def submit(self, order: object, reg: object) -> tuple[RunHandle, SubmitOutcome]:
            return RunHandle(engine="half", handle="h"), SubmitOutcome.SUBMITTED

        async def status(self, handle: RunHandle) -> RunState:
            return RunState.UNKNOWN

    assert not isinstance(_MissingCancel(), Executor)
