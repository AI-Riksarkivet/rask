"""The `Executor` port: what the platform may ask of a compute engine, in no engine's vocabulary.

docs/DECISIONS.md "The compute plane is decoupled", step 1 of the owner-ordered §7.4.

`UNKNOWN` REPLACES AN OVERLOADED `None`, and that is the substantive change rather than a rename.
`medallion.services.ray_jobs_api.job_status` returns `None` for a 404, and `medallion/workflow.py` disentangles THREE
distinct meanings from it by hand — not-yet-registered, record-lost, and a transport blip — using
`seen` / `vanished` / `never_registered` / `MAX_UNSEEN_POLLS`. A port that returns `None` forces every
caller to re-derive that.

`workflow.py` justifies `MAX_UNSEEN_POLLS=4` and `MAX_RESUBMITS=2` by an engine-specific durability
defect it names outright: "Ray's GCS is not fault-tolerant here (no external Redis, a deliberate
estate-wide `no Redis`), so a head restart takes every job record with it." Against an executor
advertising `DURABLE_RECORD`, that same machinery is a spurious DOUBLE-SUBMIT — it would run a second
copy of work the engine never lost — which is why durability is a capability the port names.
"""

from __future__ import annotations

from service_kit.lakehouse.executor import (
    Capability,
    Executor,
    RunFailure,
    RunHandle,
    RunState,
    SubmitOutcome,
)


def test_the_port_names_no_engine() -> None:
    import inspect

    from service_kit.lakehouse import executor

    source = inspect.getsource(executor).lower()
    for noun in ("ray", "spark", "flink", "dashboard", "runtime_env"):
        # `ray` appears in prose citing the defect that motivated the capability; the TYPES must not.
        assert f"class {noun}" not in source, f"the port declares an engine type {noun!r}"


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

        async def result(self, handle: RunHandle) -> object:
            # PRESENT even though this engine promises no `RESULT`. The protocol is
            # `runtime_checkable`, so a missing method makes `isinstance` answer False and the adapter
            # unreachable through the registry — the capability is what is optional, never the method.
            raise NotImplementedError

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
