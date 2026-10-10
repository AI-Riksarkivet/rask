"""The door between a planner and an out-of-process compute engine, and the one adapter that calls it (CP-056).

The lakehouse names no engine. An engine is a Dapr app-id in deployment configuration (`EngineBinding`), and every
verb the `Executor` port has reaches it as a Dapr service invocation of this module's routes:

* submit: ``POST /engine/runs`` with the run's `WorkOrder`; idempotent under the order's key, so a resubmit or a
  redelivered trigger re-attaches to the run already doing the work;
* status: ``GET /engine/runs/{key}``; ``unknown`` only when the engine answers that it holds no such run;
* failure: ``GET /engine/runs/{key}/failure``;
* cancel: ``POST /engine/runs/{key}/cancel``.

THE ORDER TRAVELS IN THE REQUEST. The call is synchronous and the engine authenticates the planner's projected token
on it, so the plan stays the one stored copy and the engine needs no read-back door into the planner.

AN UNREACHABLE ENGINE RAISES, and that is the whole of the failure semantics: the sweep isolates a raise per plan and
leaves the plan untouched, so an engine that is down for minutes costs a running job nothing. ``unknown`` is never
inferred from silence.

The run's terminal does not come through here: the job reports it on the planner's outcome door
(`run_outcomes.make_outcome_router`), as it did before the engine left the lakehouse.

Pydantic and httpx only, so the engine side imports the same wire models without the lakehouse extra.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from service_kit.lakehouse.executor import Capability, EngineError, RunFailure, RunHandle, RunState, SubmitOutcome, WrongEngineError
from service_kit.lakehouse.work_order import WorkOrder


if TYPE_CHECKING:
    from service_kit.lakehouse.task_registry import TaskRegistration


#: Where an engine app serves its runs. A run is named by its order's idempotency key.
ENGINE_RUNS_PATH: Final = "/engine/runs"


class EngineSubmission(BaseModel):
    """What a planner sends to start a run: the order, which carries everything the engine may know."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order: WorkOrder


class EngineSubmitted(BaseModel):
    """What the engine did with a submission, and the handle it answers to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: str
    outcome: SubmitOutcome


class EngineRunState(BaseModel):
    """Where a run is, in the port's vocabulary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: RunState


class EngineRunFailure(BaseModel):
    """Why a run ended badly, or ``None`` when the engine holds no cause for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failure: RunFailure | None = None


class EngineBinding(BaseModel):
    """One out-of-process engine as a deployment names it: an app to invoke and what it promises.

    ``url`` replaces the sidecar route for a deployment that runs no Dapr (a local stack, a test); empty means the
    invocation goes through this pod's sidecar to ``app_id``. ``token_file`` is this pod's projected token for the
    engine's audience, read on every call because the kubelet rotates it; empty presents none, which only an engine
    running with authentication off admits.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_id: str = Field(min_length=1)
    url: str = ""
    token_file: str = ""
    capabilities: frozenset[Capability] = frozenset()


class InvokedExecutor:
    """The `Executor` for any engine reached by service invocation. It names none: ``name`` is the deployment's key."""

    def __init__(self, name: str, binding: EngineBinding, client: httpx.AsyncClient, *, dapr_http_port: int) -> None:
        self.name = name
        self.capabilities = binding.capabilities
        self._binding = binding
        self._client = client
        self._base = binding.url.rstrip("/") if binding.url else f"http://127.0.0.1:{dapr_http_port}/v1.0/invoke/{binding.app_id}/method"

    def _headers(self) -> dict[str, str]:
        if not self._binding.token_file:
            return {}
        path = Path(self._binding.token_file)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise EngineError(f"the identity token for engine {self.name!r} at {path} could not be read") from exc
        if not token:
            raise EngineError(f"the identity token for engine {self.name!r} at {path} is empty")
        return {"Authorization": f"Bearer {token}"}

    async def _call(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """One invocation; any transport failure or non-2xx answer is the port's `EngineError`."""
        url = f"{self._base}{ENGINE_RUNS_PATH}{path}"
        try:
            response = await self._client.request(method, url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise EngineError(f"engine {self.name!r} could not be reached at {url}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise EngineError(f"engine {self.name!r} answered {response.status_code} to {method} {url}: {response.text[:300]}")
        return response.json()

    def validate_task(self, registration: TaskRegistration) -> None:
        """Refuse a registration stamped for another engine."""
        if registration.engine != self.name:
            raise WrongEngineError(f"task {registration.task!r} is registered for engine {registration.engine!r}, and this executor runs {self.name!r}")

    async def submit(self, order: WorkOrder, registration: TaskRegistration) -> tuple[RunHandle, SubmitOutcome]:
        """Start the run, or re-attach to it; the engine resolves the task to its own command.

        Raises:
            WrongEngineError: the task is registered for another engine.
            EngineError: the engine could not be reached or refused the order.
        """
        self.validate_task(registration)
        answered = EngineSubmitted.model_validate(await self._call("POST", "", body=EngineSubmission(order=order).model_dump(mode="json")))
        return RunHandle(engine=self.name, handle=answered.handle), answered.outcome

    async def status(self, handle: RunHandle) -> RunState:
        """Where the run is; ``unknown`` only when the engine says it holds no such run."""
        return EngineRunState.model_validate(await self._call("GET", f"/{handle.handle}")).state

    async def failure(self, handle: RunHandle) -> RunFailure | None:
        """The engine's classified cause, or ``None`` when it holds none."""
        return EngineRunFailure.model_validate(await self._call("GET", f"/{handle.handle}/failure")).failure

    async def cancel(self, handle: RunHandle) -> None:
        """Ask the engine to stop the run; a refused stop raises, so nobody is told a run stopped that did not."""
        await self._call("POST", f"/{handle.handle}/cancel")

    async def result(self, handle: RunHandle) -> Any:  # noqa: ANN401 — the port's own return shape; see executor.py
        """Refused: an out-of-process engine writes in another process and advertises no `Capability.RESULT`."""
        raise NotImplementedError(f"engine {self.name!r} writes out-of-process and advertises no RESULT; measure the destination of {handle.handle}")
