"""ingest's Dapr workflow adapter: the one module besides the workflow definition (`ingest.workflow`) that imports the
engine (XC-093, the `the-lakehouse-is-not-built-on-a-workflow-engine` import contract).

The app wires these onto `app.state` and its lifespan, and knows nothing of the engine's types: the runtime that
executes `ingest_run`, the starter that schedules it, the terminator and pause/resume controller, and the reader that
answers `GET /v1/ingests/{id}`. Every engine import here is lazy, so building the app needs no reachable sidecar.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import Protocol

from ingest.runs import SCHEDULE_TIMEOUT_SECONDS, ScheduleUnavailable


logger = logging.getLogger(__name__)


def start_runtime() -> object:
    """Start the Dapr WorkflowRuntime with every definition `ingest.workflow.register` lists, and return it.

    `start()` spawns the worker's own threads and does not block the event loop. Raises when the engine cannot start;
    the lifespan decides what that costs.
    """
    import dapr.ext.workflow as wf

    from ingest.workflow import register

    runtime = wf.WorkflowRuntime()
    register(runtime)
    runtime.start()
    return runtime


def stop_runtime(runtime: object) -> None:
    """Shut down a runtime `start_runtime` returned."""
    shutdown = getattr(runtime, "shutdown", None)
    if callable(shutdown):
        shutdown()


def _is_already_scheduled(exc: BaseException) -> bool:
    """True when the engine refused because it ALREADY holds this instance — a success, not a failure.

    `instance_id` is `run_id_for(project, key)`, which is deterministic, so the instance the engine is
    complaining about IS this run. The condition is reachable by design: `asyncio.wait_for` cancels
    the await, never the thread behind `to_thread`, so a schedule that was merely slow still lands and
    the retry the 503 advises then meets its own earlier dispatch. Reporting that as an error would
    make the advice impossible to satisfy; dispatching past it would run the harvest twice.

    Matched on the message because neither dapr-ext-workflow nor durabletask exports a typed
    already-exists error and the gRPC status behind it has moved between versions. Narrow enough to be
    safe: a false positive needs a schedule failure whose own text says the instance exists.
    """
    return "already exists" in str(exc).lower()


#: The transport errors, named rather than imported. `(module, attribute)` pairs resolved at call
#: time so this module keeps no import-time dependency on grpc or dapr — the same reason
#: `DaprWorkflowStarter` imports lazily — and no STATIC dependency on two libraries that ship no
#: stubs into a plane whose type gate treats a warning as a failure.
_SIDECAR_ERROR_NAMES = (("grpc", "RpcError"), ("dapr.clients.exceptions", "DaprInternalError"))


def sidecar_error_types() -> tuple[type[BaseException], ...]:
    """The exception types that mean the SIDECAR answered badly.

    Type-based rather than status-code-based on purpose. Guessing which gRPC codes are transient is
    how a permanent misconfiguration turns into an infinite client retry loop, and the code dapr
    returns for one condition has moved across versions. What is stable is the boundary: an error
    raised by the transport is an operator's problem and a caller may retry it; anything else raised
    in here — a payload that will not serialize, a name that does not resolve — is this service's own
    bug and keeps its 500.

    `OSError` covers the socket dying under the channel; there is no file I/O on this path for it to
    over-match.
    """
    types: list[type[BaseException]] = [OSError]
    for module_name, attribute in _SIDECAR_ERROR_NAMES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # pragma: no cover — both ship with dapr; absent only in a stripped env
            continue
        candidate = getattr(module, attribute, None)
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            types.append(candidate)
    return tuple(types)


class DaprWorkflowStarter:
    """Schedules `ingest_run` through the Dapr workflow client in the sidecar.

    Imported lazily inside `start` so constructing the app — which every test does — never requires a
    reachable sidecar.

    Also the place the door's failure vocabulary is DEFINED. `api.py` knows nothing about gRPC and
    should not: it maps `TimeoutError` and `ScheduleUnavailable` to a retryable 503 and lets anything
    else be a 500, so the classification has to happen at the one seam that can see a dapr exception.
    """

    async def start(self, run_id: str, payload: dict[str, object]) -> None:
        import dapr.ext.workflow as wf

        from ingest.workflow import ingest_run

        def _schedule() -> None:
            client = wf.DaprWorkflowClient()
            client.schedule_new_workflow(workflow=ingest_run, input=payload, instance_id=run_id)

        # to_thread + a timeout: the client is SYNCHRONOUS gRPC, so calling it directly would block
        # the event loop for every other request, and calling it unbounded would hold the POST open
        # past A1's one-second contract. A scheduling failure surfaces as an error on the run rather
        # than a hung connection — a caller that gets no answer cannot retry intelligently.
        try:
            await asyncio.wait_for(asyncio.to_thread(_schedule), timeout=SCHEDULE_TIMEOUT_SECONDS)
        except TimeoutError:
            # Straight through — the bound is this method's own contract and `api.py` already maps it.
            # Letting it reach the classifier below would put a decided outcome at the mercy of
            # whatever text the exception happens to carry.
            raise
        except Exception as exc:
            if _is_already_scheduled(exc):
                logger.info("run %s is already scheduled — the engine holds the instance; converging on it", run_id)
                return
            if isinstance(exc, sidecar_error_types()):
                raise ScheduleUnavailable(str(exc) or type(exc).__name__) from exc
            raise


class DaprWorkflowTerminator:
    """Stops a live run, for `POST /v1/ingests/{id}/terminate` (DWF-MGT-003).

    Before this seam existed the service exposed start and status and no lifecycle control at all, so
    a run that was WRONG rather than broken — pointed at the wrong prefix, or enumerating a bucket
    somebody meant to narrow — could not be stopped. It held its JetStream subject and its per-run
    durable and kept committing. Neither `max_units` (which refuses at enumeration, before the
    fan-out) nor `max_run_hours` (a deadline, whose in-code default is 0 = unbounded) is a brake.

    BOUNDED, NOT INSTANT, and the route says so. Terminate is recursive by default so it reaches the
    chunk children, but it stops further SCHEDULING — an in-flight activity such as a `drain_chunk`
    mid-fetch runs to completion. That is the same SDK limit `terminate_chunks` documents.

    A class rather than a bare call so it can be replaced in tests, matching the starter and reader
    beside it: the alternative is a route that cannot be exercised without a sidecar.
    """

    def terminate(self, run_id: str, reason: str = "") -> bool:
        """Ask the run to end. True when the engine accepted the request.

        RAISES AN EVENT rather than terminating the instance, and that is the whole fix.
        `terminate_workflow` sets the instance TERMINATED and never resumes the generator, so
        `emit_terminal` — the ONLY caller of `release_run_units` — never runs. The run's JetStream
        subject and its per-run durable consumer were left behind permanently (WORK_QUEUE retention
        means a message leaves only when acked, and no consumer for that run id is created again),
        and no FAIL record reached lineage: the run simply vanished. That is the
        `messages: 1, consumers: 0` state `emit_terminal`'s release comment records from the live
        estate.

        The cost, accepted knowingly (owner ruling, 2026-08-25): this is asynchronous. It asks the
        run to stop at its next select rather than stopping it, so a parent wedged before that point
        will not honour it. What it buys is ONE cleanup path — the deadline branch already does the
        right sequence, and cancellation joins it instead of inventing a second one.

        The event NAME must match `ingest.workflow.CANCEL_EVENT`; a typo hangs the run until its
        deadline with nothing saying why, which is why both sides read the one constant.
        """
        import dapr.ext.workflow as wf

        from ingest.workflow import CANCEL_EVENT

        wf.DaprWorkflowClient().raise_workflow_event(run_id, CANCEL_EVENT, data={"reason": reason})
        return True

    def pause(self, run_id: str) -> None:
        """Suspend a live run (DWF-MGT-004).

        UNLIKE TERMINATE, this is the SDK call and not an event. Terminate had to become a `cancel`
        event because `terminate_workflow` skips the rest of the generator, and the skipped tail held
        `emit_terminal` — the only caller of `release_run_units`. Pause skips nothing: the instance
        stops being scheduled and resumes exactly where it was, so the tail still runs when it does.

        The use it exists for is holding a fan-out while a credential is rotated — a run that is fine
        but must not proceed for a few minutes. Terminating that run throws away the work it has done.
        """
        import dapr.ext.workflow as wf

        wf.DaprWorkflowClient().pause_workflow(run_id)

    def resume(self, run_id: str) -> None:
        """Resume a suspended run (DWF-MGT-005).

        Shipped in the same change as `pause`, and that pairing is a contract rather than a courtesy:
        a paused instance with no way back is strictly worse than a terminated one, because it holds
        its JetStream subject and its durable consumer while making no progress at all.
        """
        import dapr.ext.workflow as wf

        wf.DaprWorkflowClient().resume_workflow(run_id)


class DaprWorkflowReader:
    """Reads a run's live state from the engine, for `GET /v1/ingests/{id}`.

    The run's truth lives in the workflow's durable history, not in this process. Reading it here —
    rather than having activities write back into a local cache — is what makes the status endpoint
    correct after a pod restart and consistent across replicas, and it removes the second writable
    copy of a state that already has an owner.
    """

    def state(self, run_id: str) -> dict[str, object] | None:
        try:
            import dapr.ext.workflow as wf

            client = wf.DaprWorkflowClient()
            state = client.get_workflow_state(run_id, fetch_payloads=True)
        except Exception:
            # No sidecar, or the instance is unknown. The caller falls back to the accepted record;
            # a status endpoint that 500s when the engine is unreachable fails at precisely the
            # moment an operator is using it to find out why.
            logger.debug("workflow state unavailable for run %s", run_id, exc_info=True)
            return None
        if state is None:
            return None
        # `to_json()`, verified against dapr-ext-workflow 1.18.3's own source: it returns
        # `runtime_status` already flattened to the enum NAME ("COMPLETED", "RUNNING", …) and
        # `serialized_output` as the JSON string the workflow returned. Reading the attributes
        # directly would work — WorkflowState.__getattr__ proxies to the wrapped object — but
        # `runtime_status` is a property that re-maps to a DIFFERENT enum, so the attribute and the
        # dict disagree on their vocabulary. One accessor, one vocabulary.
        return _with_fanout_progress(client, run_id, dict(state.to_json()))


class _WorkflowStateLike(Protocol):
    """The one method this module reads off a workflow state — see `state`'s note on `to_json()`."""

    def to_json(self) -> dict[str, object]: ...


class _WorkflowStateReader(Protocol):
    """The one method `_with_fanout_progress` needs from the SDK client.

    Protocols rather than `Any`: the helper is handed a real `DaprWorkflowClient` in production and a
    double in tests, and naming exactly what it calls is what keeps the double honest — and what lets
    `ty` narrow the `None` branch below instead of being told to look away.
    """

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _WorkflowStateLike | None: ...


def _as_custom_status(payload: object) -> dict[str, object]:
    """The custom status as a mapping — `{}` for absent, unparseable, or non-object."""
    if not isinstance(payload, str) or not payload:
        return {}
    try:
        loaded = json.loads(payload)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _with_fanout_progress(client: _WorkflowStateReader, run_id: str, payload: dict[str, object]) -> dict[str, object]:
    """Sum the CHILDREN's progress into the parent's status while the fan-out is running.

    `chunk_run` publishes per-chunk progress on its own instance, under a heading calling it "THE
    FAN-OUT'S ONLY PROGRESS SIGNAL" — and nothing read it. The parent's status for the whole fan-out
    is what it set BEFORE dispatch (`units_total`, `chunks`), with no `units_done` key, so
    `GET /v1/ingests/{id}` fell through to the accepted record's `0`. An operator watching a 10M-unit
    harvest read "0 of 10,000,000" for hours on a run landing rows the whole time — which is
    indistinguishable from a wedged run, the state this plane's terminate door exists for.

    Done on the READ side deliberately. Aggregating in the parent means racing a timer against the
    fan-out, which adds actions to `ingest_run`'s stream and so breaks replay for every in-flight
    instance. This costs nothing durable: the child ids are derived exactly as the workflow derives
    them, and the chunk count is already in the parent's own status.

    Four things it does NOT do, each for a reason:
      * not once the run is terminal — `finalize`'s output is then authoritative, and fanning out
        would be N wasted round-trips on every poll of a finished run;
      * not when the parent already carries `units_done` — it sets that itself once the fan-in
        returns, and that aggregate outranks this one;
      * not without a chunk count — before `enumerate_chunks` returns there are no children, and
        guessing an id range costs a round-trip per guess against an engine answering None to each;
      * not partially — a child read that RAISES abandons the whole sum, because an undercount would
        render as progress going backwards, which reads as corruption rather than as a failed read.
    """
    if str(payload.get("runtime_status") or "") != "RUNNING":
        return payload
    custom = _as_custom_status(payload.get("serialized_custom_status"))
    chunks = custom.get("chunks")
    if not isinstance(chunks, int) or chunks <= 0 or "units_done" in custom:
        return payload

    done = 0
    for index in range(chunks):
        try:
            child = client.get_workflow_state(f"{run_id}-c{index}", fetch_payloads=True)
        except Exception:
            logger.debug("child progress unavailable for run %s chunk %d", run_id, index, exc_info=True)
            return payload
        if child is None:
            # Scheduled as a batch, but they appear one at a time. Absent means "not started yet",
            # which is a 0, not an error.
            continue
        reported = _as_custom_status(dict(child.to_json()).get("serialized_custom_status")).get("units_done")
        if isinstance(reported, int):
            done += reported

    return {**payload, "serialized_custom_status": json.dumps({**custom, "units_done": done})}
