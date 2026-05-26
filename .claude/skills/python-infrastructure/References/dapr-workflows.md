# Dapr Workflows

Durable, code-first workflows that survive pod crashes and resume at the last completed activity. Replaces the older DBOS-based workflow patterns. Uses the Dapr sidecar — every pod that runs workflows gets a Dapr container injected (see `fastapi/references/microservices.md` § Dapr + Kubernetes interplay for the deployment side).

## Contents

- When to use Dapr Workflow vs NATS JetStream
- Install + boot
- Workflows and activities
- Retry policy on activities
- Scheduling a workflow
- Status and result
- FastAPI integration
- Critical rules — workflow determinism
- Gotchas

## When to use Dapr Workflow vs NATS JetStream

| You need… | Right tool |
| --------- | ---------- |
| A single logical workflow with **multiple non-idempotent steps** (charge → reserve → ship → notify) that must resume at the last completed step after a crash | **Dapr Workflow** — execution state persists in the sidecar's state store |
| Fan-out / work-queue / event distribution — many consumers, one message redelivered if a consumer crashes | **NATS JetStream** ([`background-jobs.md`](background-jobs.md)) |
| Cross-service event bus | **NATS JetStream** — Dapr Workflow is for *one* service's orchestration, not pub/sub |
| Saga pattern (forward + compensating actions across services) | **Dapr Workflow** — activities can publish to NATS to invoke remote work, then compensate locally on failure |
| Scheduled / cron job that's idempotent | **NATS JetStream** with a cron-publishing sidecar, or k8s `CronJob` — Dapr Workflow is overkill for stateless schedules |

Rule of thumb: if losing the message and re-running from scratch is fine, NATS. If you need "I was at step 3, resume at step 4," Dapr Workflow.

## Install + boot

```bash
uv add dapr dapr-ext-workflow
```

Workflow runtime requires the Dapr sidecar to be reachable on `127.0.0.1:50001` (gRPC) — locally via `dapr init`, in k8s via the sidecar injector (see `fastapi/references/microservices.md` § Dapr + Kubernetes).

```python
# core/workflows.py
import dapr.ext.workflow as wf

wfr = wf.WorkflowRuntime()
```

The runtime must be started before any workflow is scheduled. Start it in the FastAPI lifespan — never at module import (deferring this is hard to debug).

## Workflows and activities

A **workflow** orchestrates; **activities** do the work. The workflow function must be deterministic (see Critical rules below); activities are where real I/O happens.

```python
# core/workflows.py
import dapr.ext.workflow as wf


@wfr.workflow(name="checkout_workflow")
def checkout_workflow(ctx: wf.DaprWorkflowContext, order_id: str):
    # Each `yield` is a durable boundary — state is checkpointed here.
    validated = yield ctx.call_activity(validate_order, input=order_id)
    if not validated:
        return {"status": "invalid"}

    charge_result = yield ctx.call_activity(charge_payment, input=order_id)
    reservation = yield ctx.call_activity(reserve_inventory, input=order_id)
    yield ctx.call_activity(notify_customer, input=order_id)
    return {"status": "complete", "charge": charge_result, "reservation": reservation}


@wfr.activity(name="validate_order")
def validate_order(ctx: wf.WorkflowActivityContext, order_id: str) -> bool:
    # Real DB call — activities are NOT subject to the determinism rule.
    return order_repository.exists(order_id)


@wfr.activity(name="charge_payment")
def charge_payment(ctx: wf.WorkflowActivityContext, order_id: str) -> dict[str, object]:
    return payment_service.charge(order_id)
```

Each `yield ctx.call_activity(...)` is a **durable checkpoint**: the result is persisted to the Dapr state store. If the pod crashes after that yield, on resume the workflow replays everything up to that point from the checkpoint log (no re-execution of completed activities) and continues from the next yield.

## Retry policy on activities

Apply retries at the activity boundary, not inside the activity. The workflow engine handles backoff and persistence.

```python
from datetime import timedelta
import dapr.ext.workflow as wf


retry = wf.RetryPolicy(
    max_number_of_attempts=5,
    first_retry_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    max_retry_interval=timedelta(minutes=2),
)


@wfr.workflow(name="resilient_checkout")
def resilient_checkout(ctx: wf.DaprWorkflowContext, order_id: str):
    return (yield ctx.call_activity(charge_payment, input=order_id, retry_policy=retry))
```

Pick `max_number_of_attempts` so total elapsed wait stays under your business deadline. The workflow keeps state across retries — `charge_payment` will only be re-invoked if it raises.

## Scheduling a workflow

The client is independent of the runtime — you can schedule from any process that can reach the Dapr sidecar.

```python
# api/routes/checkout.py
import dapr.ext.workflow as wf

from app.core.workflows import checkout_workflow


client = wf.DaprWorkflowClient()


async def start_checkout(order_id: str) -> str:
    return client.schedule_new_workflow(
        workflow=checkout_workflow,
        input=order_id,
        instance_id=f"checkout-{order_id}",   # OPTIONAL but recommended for idempotency
    )
```

Passing an explicit `instance_id` makes scheduling **idempotent** — a duplicate call with the same id returns the existing instance instead of starting a second one. Without it, every call starts a new workflow.

## Status and result

```python
state = client.get_workflow_state(instance_id=f"checkout-{order_id}")

state.runtime_status   # RUNNING / COMPLETED / FAILED / TERMINATED / SUSPENDED
state.serialized_output  # JSON of the workflow's return value once COMPLETED
state.failure_details    # error type, message, stack trace if FAILED
```

For long-running workflows, expose `get_workflow_state` via a FastAPI route so clients can poll, or use `client.wait_for_workflow_completion(...)` for short ones.

## FastAPI integration

The workflow runtime is a long-lived background process — start it in lifespan, stop on shutdown.

```python
# main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.core.workflows import wfr


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... db_engine, http, redis, nats ...
    wfr.start()
    app.state.workflow_client = wf.DaprWorkflowClient()
    yield
    wfr.shutdown()


app = FastAPI(lifespan=lifespan)
```

```python
# api/deps.py
from typing import Annotated
import dapr.ext.workflow as wf
from fastapi import Depends, Request


def get_workflow_client(request: Request) -> wf.DaprWorkflowClient:
    return request.app.state.workflow_client


WorkflowClientDep = Annotated[wf.DaprWorkflowClient, Depends(get_workflow_client)]
```

```python
# api/routes/checkout.py
@router.post("/checkout/{order_id}")
async def start_checkout(order_id: str, client: WorkflowClientDep) -> dict[str, str]:
    instance_id = client.schedule_new_workflow(
        workflow=checkout_workflow, input=order_id, instance_id=f"checkout-{order_id}",
    )
    return {"instance_id": instance_id, "status_url": f"/checkout/{order_id}/status"}


@router.get("/checkout/{order_id}/status")
async def checkout_status(order_id: str, client: WorkflowClientDep) -> dict[str, object]:
    state = client.get_workflow_state(instance_id=f"checkout-{order_id}")
    return {"status": state.runtime_status.name, "output": state.serialized_output}
```

## Critical rules — workflow determinism

Workflow functions are **replayed** on every crash recovery. Anything non-deterministic inside the workflow function corrupts state. **Activities are not subject to this rule** — only the workflow function itself.

| ❌ Inside a workflow function | ✅ Move it to an activity |
| ----------------------------- | ------------------------- |
| `datetime.now()` / `time.time()` | `yield ctx.call_activity(get_current_time)` — or use `ctx.current_utc_datetime` (replay-safe) |
| `random.random()` / `uuid.uuid4()` | `ctx.new_uuid()` (replay-safe), or activity |
| `httpx.get(...)` | Activity |
| `await session.execute(...)` (any DB call) | Activity |
| `os.environ[...]` reads | Pass via input or activity |
| `print(...)` / `log.info(...)` with side-effects | Activity (logs replay too, so they double — use `ctx.is_replaying` to guard) |
| File I/O | Activity |

The rule is mechanical: **if it touches the outside world or returns different values on different calls, it goes in an activity**. The workflow function is glue — `if`/`else`, loops, `yield` calls.

## Gotchas

- **Workflow runtime needs the sidecar.** Locally you need `dapr init` to run; in k8s the sidecar must be injected (see `fastapi/references/microservices.md`). Without the sidecar, `wfr.start()` will appear to succeed but no workflows execute.
- **`wfr.start()` is non-blocking.** It spawns a background thread. Don't `await` it; do call `wfr.shutdown()` in lifespan teardown or it leaks.
- **Workflow inputs must be JSON-serializable.** Pydantic `model_dump_json()` is fine; passing a SQLAlchemy row or a custom class fails opaquely.
- **Activities run on the Dapr placement service's selection of a worker** — they may run on a different pod than the workflow. Don't assume the activity sees the same in-process state.
- **One workflow runtime per process.** Multiple `WorkflowRuntime()` instances in the same process race; create one in `core/workflows.py` and import it.
- **Replays double the log volume.** Wrap `log.info(...)` calls with `if not ctx.is_replaying:` if you want one log per actual execution, not one per replay.
- **Activity functions are looked up by name** (the `name=` argument), not by Python import path. Renaming an activity in flight breaks any in-progress workflow that already checkpointed a call to the old name. Add new activities; don't rename existing ones until you've drained the queue.
- **No nested workflows for cross-service orchestration.** `ctx.call_child_workflow(...)` is for *this* service. For cross-service work, the parent workflow's activity publishes to NATS and waits for an event back via `ctx.wait_for_external_event(...)`.

## Cross-skill boundaries

- **Sidecar deployment + k8s annotations**: `fastapi/references/microservices.md` § Dapr + Kubernetes interplay
- **FastAPI lifespan + dependency injection**: `fastapi/references/production-patterns.md` § Lifespan
- **Pub/sub across services**: [`background-jobs.md`](background-jobs.md) for `nats-py` direct, OR `fastapi/references/microservices.md` § Dapr + Kubernetes for the **`pubsub.jetstream`** Dapr component (Dapr SDK routes to the same NATS broker via the sidecar — preferred when the sidecar is already deployed for Workflow)
- **Tracing across workflow → activity → external call**: [`observability.md`](observability.md) — Dapr propagates W3C trace context automatically when it owns the call
