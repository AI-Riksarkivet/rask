# Background Jobs & Task Queues

Decouple long-running or unreliable work from request/response cycles. This project uses **NATS JetStream** (via `nats-py`) as the primary task queue.

> **JetStream vs Dapr Workflow — pick the right tool.** JetStream gives you _message durability_: the message survives, handlers are idempotent, retry means redelivering the whole message. **Dapr Workflow** (`dapr-workflows.md`) gives you _workflow durability_: the function's execution state survives crashes; on resume it picks up at the last completed activity. Use JetStream for fanout/work-queue/event distribution. Reach for Dapr Workflow when one logical workflow has multiple non-idempotent steps and you can't safely re-run from step 1 (checkout: charge → reserve → ship → notify). Dapr Workflow requires a Dapr sidecar per pod (see `fastapi/references/microservices.md` § Dapr + Kubernetes).

## Contents

- Core concepts
- Connecting (`nats-py`)
- Stream + consumer setup (one-time, idempotent)
- Return a job ID immediately
- Make tasks idempotent
- Job state management
- Dead letter handling
- Status polling endpoint (FastAPI)
- Worker shape (asyncio pull consumer)
- Push consumer (when you want NATS to drive)
- Summary

## Core concepts

1. **Task queue pattern.** API accepts request, enqueues a job, returns immediately with a job ID. Workers process jobs asynchronously.
2. **Idempotency.** Tasks may be retried on failure. Design for safe re-execution.
3. **Job state machine.** Jobs transition through `pending → running → succeeded/failed`.
4. **At-least-once delivery.** Most queues guarantee at-least-once. Code must handle duplicates.

## Connecting (`nats-py`)

Install: `uv add nats-py`. One NATS connection per process; reuse it everywhere.

```python
import nats
from nats.aio.client import Client as NATS
from nats.js import JetStreamContext


async def connect_nats(servers: list[str]) -> tuple[NATS, JetStreamContext]:
    nc = await nats.connect(
        servers=servers,
        allow_reconnect=True,
        max_reconnect_attempts=-1,   # retry forever; let orchestrator decide when to give up
        reconnect_time_wait=2,
        ping_interval=20,
        max_outstanding_pings=3,
    )
    js = nc.jetstream()
    return nc, js
```

Drain (don't `.close()`) on shutdown so in-flight publishes/acks finish:

```python
await nc.drain()
```

## Stream + consumer setup (one-time, idempotent)

```python
from nats.js.api import (
    StreamConfig, ConsumerConfig,
    RetentionPolicy, StorageType, AckPolicy, DeliverPolicy,
)


async def ensure_stream(js: JetStreamContext) -> None:
    await js.add_stream(StreamConfig(
        name="EXPORTS",
        subjects=["exports.>"],
        retention=RetentionPolicy.WORK_QUEUE,   # message deleted on ack
        storage=StorageType.FILE,
        num_replicas=3,
        max_age=24 * 60 * 60,                    # expire stale tasks (1 day)
        duplicate_window=120,                    # dedupe within 2 min via Nats-Msg-Id
    ))

    await js.add_consumer("EXPORTS", ConsumerConfig(
        durable_name="export-processor",
        ack_policy=AckPolicy.EXPLICIT,
        ack_wait=60,                             # seconds
        max_deliver=5,
        max_ack_pending=100,
        filter_subject="exports.>",
        deliver_policy=DeliverPolicy.ALL,
    ))
```

`add_stream`/`add_consumer` are safe to call at startup — they update in place if the config matches.

## Return a job ID immediately

For operations that take more than a few seconds, persist a job record, enqueue the work, return a poll URL.

```python
import json
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Job(BaseModel):
    id: str
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict | None = None
    error: str | None = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    poll_url: str


async def start_export(
    request: ExportRequest,
    js: JetStreamContext,
) -> JobResponse:
    """Start export job and return job ID."""
    job_id = str(uuid4())

    await jobs_repo.create(
        Job(
            id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )
    )

    payload = json.dumps({"job_id": job_id, "params": request.model_dump()}).encode()

    # Nats-Msg-Id enables JetStream-side dedup within `duplicate_window`.
    ack = await js.publish(
        "exports.process",
        payload,
        headers={"Nats-Msg-Id": job_id},
    )
    # ack.seq is the stream sequence — log it for traceability.

    return JobResponse(job_id=job_id, status="pending", poll_url=f"/jobs/{job_id}")
```

## Make tasks idempotent

Workers may retry on crash or timeout. Design for safe re-execution.

```python
async def process_order(order_id: str) -> None:
    """Process order idempotently."""
    order = await orders_repo.get(order_id)

    # Already processed? Return early
    if order.status == OrderStatus.COMPLETED:
        log.info("order_already_processed", extra={"order_id": order_id})
        return

    # Process with an idempotency key passed to the payment provider
    await payment_provider.charge(
        amount=order.total,
        idempotency_key=f"order-{order_id}",  # critical
    )

    await orders_repo.update(order_id, status=OrderStatus.COMPLETED)
```

**Idempotency strategies:**

1. **Check-before-write** — verify state before action.
2. **Idempotency keys** — unique tokens passed to external services.
3. **Upsert patterns** — `INSERT ... ON CONFLICT UPDATE`.
4. **Deduplication window** — track processed IDs for N hours (Redis is a natural fit).

## Job state management

Persist transitions for visibility and debugging.

```python
class JobRepository:
    """Repository for job state."""

    async def create(self, job: Job) -> Job:
        await self._db.execute(
            """INSERT INTO jobs (id, status, created_at)
               VALUES ($1, $2, $3)""",
            job.id, job.status.value, job.created_at,
        )
        return job

    async def update_status(
        self,
        job_id: str,
        status: JobStatus,
        **fields,
    ) -> None:
        updates = {"status": status.value, **fields}
        now = datetime.now(timezone.utc)
        if status == JobStatus.RUNNING:
            updates["started_at"] = now
        elif status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
            updates["completed_at"] = now

        # ... build/exec UPDATE ...

        log.info("job_status_updated", extra={"job_id": job_id, "status": status.value})
```

## Dead letter handling

When retries exhaust, capture the failure for manual inspection rather than dropping it silently. In JetStream this is typically a separate stream/subject (e.g. `exports.dlq`) consumed by an operator tool.

```python
async def handle_failure(
    *,
    job_id: str,
    payload: dict,
    error: Exception,
    attempts: int,
) -> None:
    await dead_letter_queue.publish(
        subject="exports.dlq",
        payload={
            "job_id": job_id,
            "payload": payload,
            "error": f"{type(error).__name__}: {error}",
            "attempts": attempts,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    log.error(
        "job_dlq",
        extra={"job_id": job_id, "attempts": attempts, "error": str(error)},
    )
```

## Status polling endpoint (FastAPI)

```python
from fastapi import HTTPException
from pydantic import BaseModel


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    result: dict | None
    error: str | None
    is_terminal: bool


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    job = await jobs_repo.get(job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id} not found")

    return JobStatusResponse(
        job_id=job.id,
        status=job.status.value,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        result=job.result if job.status == JobStatus.SUCCEEDED else None,
        error=job.error if job.status == JobStatus.FAILED else None,
        is_terminal=job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED),
    )
```

## Worker shape (asyncio pull consumer)

Pull consumers give backpressure: the worker fetches a batch when it's ready for more.

```python
import asyncio
import json
import logging

log = logging.getLogger(__name__)


class RetryableError(Exception):
    """Worker raises this when the job should be redelivered (transient failure)."""


async def run_worker(js: JetStreamContext) -> None:
    """Consume export jobs and process them."""
    psub = await js.pull_subscribe("exports.>", durable="export-processor")

    while True:
        try:
            msgs = await psub.fetch(batch=10, timeout=5)
        except asyncio.TimeoutError:
            continue  # no work right now
        except Exception as e:
            log.error("fetch_failed", extra={"error": str(e)})
            await asyncio.sleep(1)
            continue

        for msg in msgs:
            await _handle(msg)


async def _handle(msg) -> None:
    payload = json.loads(msg.data.decode())
    job_id = payload["job_id"]
    attempts = msg.metadata.num_delivered

    await jobs_repo.update_status(job_id, JobStatus.RUNNING)

    try:
        result = await process_job(payload)
        await jobs_repo.update_status(job_id, JobStatus.SUCCEEDED, result=result)
        await msg.ack()
    except RetryableError as e:
        # Let JetStream redeliver per the consumer's ack_wait / max_deliver.
        log.warning("job_retryable", extra={"job_id": job_id, "error": str(e)})
        await msg.nak(delay=5)
    except ValueError as e:
        # Permanent failure (bad input) — never redeliver.
        await jobs_repo.update_status(job_id, JobStatus.FAILED, error=f"ValueError: {e}")
        await handle_failure(job_id=job_id, payload=payload, error=e, attempts=attempts)
        await msg.term()
    except Exception as e:
        # Unknown failure. If we're at max_deliver, dead-letter; otherwise let it redeliver.
        if attempts >= 5:
            await jobs_repo.update_status(job_id, JobStatus.FAILED, error=f"{type(e).__name__}: {e}")
            await handle_failure(job_id=job_id, payload=payload, error=e, attempts=attempts)
            await msg.term()
        else:
            await msg.nak(delay=2 ** attempts)
```

For long-running work, call `msg.in_progress()` before each slow step to extend the `ack_wait` deadline so JetStream doesn't redeliver behind your back.

Wrap `process_job` with the resilience patterns in `resilience.md` (retries on transient errors only). Wrap with OTel spans per `observability.md`. Across queue boundaries, propagate trace context via message headers (also covered in `observability.md`).

## Push consumer (when you want NATS to drive)

Use a push consumer for low-latency event listeners (notifications, audit log). Pull is the default for everything else.

```python
async def order_handler(msg) -> None:
    order = json.loads(msg.data.decode())
    try:
        await send_confirmation_email(order)
        await msg.ack()
    except Exception:
        await msg.nak()


sub = await js.subscribe(
    "orders.>",
    durable="notification-sender",
    cb=order_handler,
    manual_ack=True,
)
```

## Summary

1. **Return immediately** — don't block requests for long operations.
2. **Persist job state** — enable polling and debugging.
3. **Idempotent tasks** — safe to retry on any failure.
4. **Use idempotency keys** for external service calls.
5. **Set timeouts** — both soft and hard.
6. **DLQ permanent failures** — separate stream/subject in JetStream.
7. **Log transitions** — every state change.
8. **Exponential backoff** for retries; never bare retry loops.
9. **Don't retry permanent failures** — validation, auth, 4xx (except 429).
10. **Propagate trace context** through message headers (see `observability.md`).
