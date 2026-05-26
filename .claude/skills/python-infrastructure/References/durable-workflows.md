# Durable Workflows (DBOS)

DBOS is a Python durable-execution framework. Decorate functions as workflows and steps; DBOS persists step-by-step progress to Postgres (or SQLite) so that when a worker crashes mid-workflow, it resumes from the last completed step instead of re-running the whole thing.

## When to use DBOS vs JetStream

| You have…                                                                                       | Use                                          |
| ----------------------------------------------------------------------------------------------- | -------------------------------------------- |
| Events to fan out, work to queue, messages to publish/consume                                   | **NATS JetStream** (`background-jobs.md`)    |
| One logical workflow with multiple non-idempotent steps that can't safely be re-run from step 1 | **DBOS** (this file)                         |
| Both — events feed a multi-step workflow                                                        | JetStream consumer kicks off a DBOS workflow |

Examples that justify DBOS:

- **Checkout** — charge card → reserve inventory → ship → notify. Re-charging on retry is bad; re-running the workflow naively double-charges.
- **Saga with compensation** — multi-service transaction with explicit rollback steps.
- **Long-running scheduled work** — daily backup, periodic reconciliation, anything where the schedule itself must survive a crash.
- **Human-in-the-loop** — workflow pauses for a webhook (payment confirmation) and must resume hours/days later.

Examples that don't justify DBOS:

- Sending a single email, posting a webhook, processing one image — a JetStream task + idempotent handler is simpler.
- High-throughput fanout of events to many consumers — JetStream is the right tool.

## Install + boot

```bash
uv add dbos
```

DBOS needs a Postgres URL for its system database. Use `pydantic-settings` (see `writing-python/References/configuration.md`) to source it from the environment.

```python
import os
from dbos import DBOS, DBOSConfig

config: DBOSConfig = {
    "name": "rask-workflows",
    "system_database_url": os.environ["DBOS_SYSTEM_DATABASE_URL"],
    "application_database_url": os.environ.get("APP_DATABASE_URL"),  # optional
    "log_level": "INFO",
    "application_version": os.environ.get("APP_VERSION", "dev"),
}
DBOS(config=config)
```

Call `DBOS.launch()` once at process start, **after** all `@DBOS.workflow` / `@DBOS.step` decorators have been imported, and **before** invoking any workflow.

## Workflows, steps, transactions

| Decorator             | Purpose                                                                             |
| --------------------- | ----------------------------------------------------------------------------------- |
| `@DBOS.workflow()`    | Durable, recoverable execution unit. Resumes from last completed step on crash.     |
| `@DBOS.step()`        | Non-deterministic operation (I/O, time, randomness). Step output is checkpointed.   |
| `@DBOS.transaction()` | A step that's executed inside a single Postgres transaction via `DBOS.sql_session`. |
| `@DBOS.scheduled()`   | Cron-scheduled workflow.                                                            |

```python
import requests
from dbos import DBOS

@DBOS.step()
def call_external_api(data: str) -> dict:
    resp = requests.post("https://api.example.com", json={"data": data})
    resp.raise_for_status()
    return resp.json()

@DBOS.step()
def derive_status(result: dict) -> str:
    return result.get("status", "unknown")

@DBOS.workflow()
def my_workflow(input_data: str) -> str:
    api_result = call_external_api(input_data)
    return derive_status(api_result)
```

When `my_workflow` crashes after `call_external_api` completes but before `derive_status` runs, DBOS resumes from `derive_status` with the persisted `api_result` — the external API is NOT called again.

### Steps with retry

```python
@DBOS.step(retries_allowed=True, max_attempts=10)
def unreliable_api_call(url: str) -> str:
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.text
```

### Database transactions

Use `@DBOS.transaction()` for DB writes that must be atomic with workflow checkpointing.

```python
from sqlalchemy import Table, Column, String, MetaData

metadata = MetaData()
users = Table(
    "users", metadata,
    Column("id", String, primary_key=True),
    Column("name", String),
)

@DBOS.transaction()
def create_user(user_id: str, name: str) -> None:
    DBOS.sql_session.execute(users.insert().values(id=user_id, name=name))

@DBOS.transaction()
def get_user(user_id: str) -> dict | None:
    row = DBOS.sql_session.execute(
        users.select().where(users.c.id == user_id)
    ).fetchone()
    return dict(row._mapping) if row else None

@DBOS.workflow()
def user_workflow(user_id: str, name: str) -> dict | None:
    create_user(user_id, name)
    return get_user(user_id)
```

## Background workflows

```python
from dbos import WorkflowHandle

@DBOS.workflow()
def background_workflow(data: str) -> str:
    return long_running_task(data)

handle: WorkflowHandle = DBOS.start_workflow(background_workflow, "input")
# ...later, possibly in a different process...
result = handle.get_result()
```

## Queues

DBOS queues let you bound worker concurrency or rate-limit calls into a backend.

```python
from dbos import Queue

task_queue = Queue("tasks", worker_concurrency=5)

rate_limited = Queue(
    "external-api",
    limiter={"limit": 100, "period": 60},   # 100 calls / minute
)

@DBOS.workflow()
def process_task(task_id: int) -> str:
    return f"done {task_id}"

handle = task_queue.enqueue(process_task, 123)
result = handle.get_result()
```

### Partitioned queues (per-key ordering)

```python
user_queue = Queue("user_actions", worker_concurrency=10)

@DBOS.workflow()
def process_user_action(user_id: str, action: dict) -> str:
    return f"processed {action} for {user_id}"

# Actions for the same user_id execute in order; different users in parallel.
user_queue.enqueue(
    process_user_action, "user_123", {"type": "update"},
    partition_key="user_123",
)
```

## Idempotency

Same workflow ID = same execution. The second call returns the first call's result instead of running again.

```python
from dbos import SetWorkflowID

@DBOS.workflow()
def process_payment(payment_id: str, amount: float) -> str:
    return "processed"

def handle_payment(payment_id: str, amount: float) -> str:
    with SetWorkflowID(f"payment-{payment_id}"):
        return process_payment(payment_id, amount)
```

## Timeout

```python
from dbos import SetWorkflowTimeout

@DBOS.workflow()
def long_workflow(data: str) -> str:
    return "completed"

with SetWorkflowTimeout(3600):   # cancel if it runs longer than 1 hour
    result = long_workflow("input")
```

## Workflow communication

### Messages (workflow waits for external signal)

```python
@DBOS.workflow()
def checkout_workflow(order_id: str) -> str:
    payment = DBOS.recv(topic="payment", timeout_seconds=300)
    if payment is None:
        return "payment_timeout"
    return "order_completed" if payment["status"] == "completed" else "payment_failed"


def payment_webhook(workflow_id: str, status: str) -> None:
    DBOS.send(workflow_id, {"status": status}, topic="payment")
```

### Events (workflow publishes value for outside reader)

```python
@DBOS.workflow()
def checkout_workflow(order: dict) -> str:
    payment_url = generate_payment_url(order)
    DBOS.set_event("payment_url", payment_url)
    return "completed"


def checkout_handler(order: dict) -> dict:
    handle = DBOS.start_workflow(checkout_workflow, order)
    url = DBOS.get_event(handle.workflow_id, "payment_url", timeout_seconds=30)
    return {"redirect_url": url}
```

## Durable sleep

`DBOS.sleep` survives process restarts. The workflow resumes when the sleep elapses, even on a different worker.

```python
@DBOS.workflow()
def scheduled_task_workflow(delay_seconds: int, task: str) -> str:
    DBOS.sleep(delay_seconds)
    return execute_task(task)
```

## Cron / scheduled workflows

```python
@DBOS.scheduled("0 2 * * *")        # daily 02:00
@DBOS.workflow()
def daily_backup(scheduled_time, actual_time) -> str:
    perform_backup()
    return "backup_completed"

@DBOS.scheduled("*/15 * * * *")     # every 15 min
@DBOS.workflow()
def health_check(scheduled_time, actual_time) -> str:
    return check_system_health()
```

Schedules start automatically once `DBOS.launch()` is called.

## Async workflows

DBOS supports `async def` workflows and steps; use `DBOS.sleep_async` instead of `DBOS.sleep`.

```python
import aiohttp

@DBOS.step()
async def async_fetch(url: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.text()

@DBOS.workflow()
async def async_workflow(urls: list[str]) -> list[str]:
    await DBOS.sleep_async(1)
    return [await async_fetch(u) for u in urls]
```

## FastAPI integration

A FastAPI route can be a DBOS workflow directly. State persists across restarts.

```python
from fastapi import FastAPI
import uvicorn

app = FastAPI()

@DBOS.step()
def process_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "processed"}

@app.post("/orders/{order_id}")
@DBOS.workflow()
def create_order(order_id: str) -> dict:
    return process_order(order_id)

@app.get("/orders/{order_id}/status")
def get_order_status(order_id: str):
    workflows = DBOS.list_workflows(workflow_id=order_id)
    return workflows[0] if workflows else None

if __name__ == "__main__":
    DBOS.launch()
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

For the rest of the FastAPI conventions (router structure, DI, response models), see the `fastapi` skill.

## Class-based workflows

Use when state needs to be configured per instance (e.g. one processor per data source).

```python
from dbos import DBOSConfiguredInstance

@DBOS.dbos_class()
class DataProcessor(DBOSConfiguredInstance):
    def __init__(self, source_url: str) -> None:
        self.source_url = source_url
        super().__init__(config_name=source_url)

    @DBOS.step()
    def fetch_data(self) -> dict:
        return requests.get(self.source_url).json()

    @DBOS.workflow()
    def process(self) -> dict:
        data = self.fetch_data()
        return {"transformed": data}


# Construct BEFORE DBOS.launch()
processor = DataProcessor("https://api.example.com/data")

if __name__ == "__main__":
    DBOS.launch()
    result = processor.process()
```

## Introspection & control

```python
pending   = DBOS.list_workflows(status="PENDING", limit=10)
completed = DBOS.list_workflows(status="SUCCESS", sort_desc=True)

steps     = DBOS.list_workflow_steps(workflow_id)

DBOS.cancel_workflow(workflow_id)
DBOS.resume_workflow(workflow_id)
DBOS.fork_workflow(workflow_id, start_step=3)
```

## Testing

```python
import pytest
from dbos import DBOS, DBOSConfig

@pytest.fixture
def dbos_test():
    DBOS.destroy()
    DBOS(config=DBOSConfig(name="test-app"))
    DBOS.reset_system_database()
    DBOS.launch()
    yield
    DBOS.destroy()

def test_workflow(dbos_test):
    assert my_workflow("test_input") == "expected_output"
```

## Critical rules

**Workflows must be deterministic.** DBOS replays workflows from checkpoints; non-deterministic operations would produce different results on replay. So:

1. **No direct randomness in workflows** — `random.randint` etc. must live in a `@DBOS.step()`.
2. **No direct time access in workflows** — use `DBOS.sleep` / `DBOS.sleep_async`, or wrap `time.time()` in a step.
3. **No direct I/O in workflows** — every API call, file read, DB write goes in a step or transaction.
4. **No threads in workflows** — use child workflows or queues for concurrency.

**Steps:**

- Inputs and outputs must be JSON-serializable (Pydantic models are fine via `model_dump`).
- Should be idempotent (workflows resume; steps can be re-run on retry).
- Use `retries_allowed=True` for transient failures.

**Workflows:**

- Decorated with `@DBOS.workflow()`.
- Inputs and return value JSON-serializable.
- `DBOS.launch()` must be called before any workflow runs.

**Transactions:**

- `@DBOS.transaction()` is for DB ops only.
- Access the session via `DBOS.sql_session` (SQLAlchemy).

## Cross-skill boundaries

- **`fastapi`** — HTTP routing, request models. Pair `@app.post(...)` with `@DBOS.workflow()` to make endpoints durable.
- **`writing-python` → `configuration.md`** — source `DBOS_SYSTEM_DATABASE_URL` via `pydantic-settings`.
- **`background-jobs.md`** (this skill) — for pure message queueing / fanout, use NATS JetStream instead. JetStream consumers can kick off DBOS workflows when one inbound message needs a durable multi-step response.
- **`observability.md`** (this skill) — wrap step entry/exit with OTel spans; DBOS workflow IDs make natural span attributes.

## Gotchas

- **Workflows can't `await` arbitrary tasks** — anything async/non-deterministic must be a step.
- **Changing a workflow's signature breaks resumption** for in-flight executions. Version workflows when changing inputs/steps.
- **Postgres is mandatory in production.** SQLite mode is for local dev only.
- **`DBOS.launch()` ordering** — late imports of workflow modules after `launch()` won't register them.
- **Queue worker concurrency is per-process.** If you run N replicas, total parallelism is `N × worker_concurrency`.
- **`SetWorkflowID` collisions are silent** — passing the same ID returns the original result. Useful for idempotency, dangerous if accidental.
