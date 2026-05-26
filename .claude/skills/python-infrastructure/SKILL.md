---
name: python-infrastructure
description: Python patterns for system reliability — background jobs and task queues (NATS JetStream via nats-py), durable multi-step workflows (Dapr Workflow via dapr-ext-workflow), resilience and recovery (retries, backoff, timeouts, circuit breakers via tenacity), caching (Redis), and observability (OpenTelemetry traces, metrics, logs via OTLP). USE WHEN building async workers, queueing tasks, designing fault-tolerant multi-step workflows that must survive crashes, handling transient network/IO failures, instrumenting Python services for production, designing retry policies, configuring tracing/metrics, or caching with Redis. NOT FOR language idioms or type hygiene (use `writing-python`), HTTP routing (use `fastapi`), or deep OTel reference (use `otel`).
---

# Python Infrastructure

System-reliability concerns for Python services in this project, grouped because real code uses them together: a task you queue (background-jobs) needs retries (resilience), instrumentation (observability), and often touches the cache (Redis) on the same call path.

## Preferred stack

| Concern                      | Tool                               | Notes                                                                                                                                                         |
| ---------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Message bus / task queue     | **NATS JetStream** (via `nats-py`) | Durable streams, consumer groups, replay. Replaces Celery/RabbitMQ here.                                                                                      |
| Durable multi-step workflows | **Dapr Workflow** (via `dapr-ext-workflow`) | Activity-level checkpointing via the Dapr sidecar. Use only when a workflow has multiple non-idempotent steps and JetStream's "redeliver the whole message" model isn't enough. Requires a Dapr sidecar per pod (see `fastapi/references/microservices.md` § Dapr + Kubernetes). |
| HTTP                         | **FastAPI**                        | See sibling `fastapi` skill.                                                                                                                                  |
| Cache                        | **Redis**                          | `redis.asyncio` for async workers.                                                                                                                            |
| Retries / backoff            | **tenacity**                       | Exponential + jitter, by default.                                                                                                                             |
| Observability                | **OpenTelemetry** (OTLP)           | Traces + metrics + logs. See sibling `otel`.                                                                                                                  |
| Logging                      | stdlib `logging` → OTel handler    | Don't pull in `structlog`; OTel forwards stdlib records.                                                                                                      |
| HTTP client                  | **httpx** (async)                  | Replaces `requests`.                                                                                                                                          |

## Scope routing

| If you need to…                                                                                                 | Read                              |
| --------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| Queue a task, design a worker, persist job state, retry/DLQ patterns (NATS JetStream + `nats-py`)               | `References/background-jobs.md`   |
| Survive crashes mid-workflow with activity-level recovery (Dapr Workflow — workflows, activities, retry policies, scheduling)        | `References/dapr-workflows.md`    |
| Decide what to retry, with what backoff, when to stop, circuit-breakers                                         | `References/resilience.md`        |
| Instrument a service with OTel traces/metrics/logs, four golden signals                                         | `References/observability.md`     |
| Use Redis as a cache (TTL, invalidation, async client patterns)                                                 | `References/caching.md`           |

## Decision tree

```
Operation can fail transiently (network/IO/3rd-party API)?
  -> resilience.md (retry policy)

Operation runs out-of-request (email, image processing, batch)?
  Is the work a single idempotent action? Or fanout/event distribution?
    -> background-jobs.md (NATS JetStream)
  Is it a multi-step workflow where re-running step 1 on crash is bad
  (charge -> reserve -> ship -> notify)?
    -> dapr-workflows.md (Dapr Workflow — sidecar required)

Need to know what's happening in production?
  -> observability.md (OTel)

Need to avoid repeated expensive lookups?
  -> caching.md (Redis)

All five at once for one feature?
  -> instrument first, then queue / workflow + retry + cache.
```

## Cross-skill boundaries

- **`writing-python`** — _how_ to write the function. This skill — _how it survives in production_.
- **`writing-python` → `References/error-handling.md`** — _what exception to raise_. This skill — _what to do when it's raised across a network boundary_.
- **`writing-python` → `References/resource-management.md`** — _how to clean up resources_ (context managers). This skill — _how to keep retrying when resources fail to acquire_.
- **`fastapi`** — request handlers and DI. This skill — what runs around them.
- **`otel`** — full OTel reference (Python SDK, signals, attributes, Collector). This skill's `observability.md` pins project conventions on top.

## Top gotchas

- **Retry without backoff is a DoS amplifier.** A failed downstream + immediate retry from N clients = traffic burst that keeps the downstream down. Default to exponential backoff + jitter from day one.
- **Retrying non-idempotent operations duplicates side effects.** A failed POST + retry can mean two charges. Always pair retry with an idempotency key OR mark the operation non-retryable.
- **Synchronous code inside an async worker blocks the event loop.** A "fast" `requests` call in an asyncio worker kills throughput. Use `httpx.AsyncClient` or run sync code in an executor.
- **Logs and metrics serve different audiences.** Logs answer "what happened to this one request"; metrics answer "what's happening across all requests". Don't try to derive one from the other — instrument both.
- **Trace context propagation needs explicit plumbing across queue boundaries.** Publishing to JetStream loses the current trace unless you serialize trace context into message headers and restore it in the consumer. The OTel propagation API (`inject`/`extract`) is the supported path.
- **Redis cache stampede** — N clients miss the same key simultaneously and all recompute. Use single-flight locks or `SETNX`-based mutexes for hot keys.
