# Resilience Patterns

Retries, exponential backoff with jitter, timeouts, fault-tolerant decorators. `tenacity` is the default.

## Contents

- Basic retry
- Retry only appropriate errors
- HTTP status code retries
- Combined exception and status retry
- Logging retry attempts
- Timeout decorator
- Stack cross-cutting concerns
- Inject infrastructure for testability
- Fail-safe defaults
- Circuit breakers
- Summary

## Basic retry

```python
from tenacity import (
    retry,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential_jitter,
    retry_if_exception_type,
)
import httpx

TRANSIENT_ERRORS = (ConnectionError, TimeoutError, OSError)

@retry(
    retry=retry_if_exception_type(TRANSIENT_ERRORS),
    stop=stop_after_attempt(5) | stop_after_delay(60),
    wait=wait_exponential_jitter(initial=1, max=30),
)
def fetch_data(url: str) -> dict:
    """Fetch data with automatic retry on transient failures."""
    response = httpx.get(url, timeout=30)
    response.raise_for_status()
    return response.json()
```

## Retry only appropriate errors

Never retry:

- `ValueError`, `TypeError` — bugs, not transient
- Authentication errors — invalid credentials won't become valid
- HTTP 4xx except 429 — client errors are permanent

```python
RETRYABLE_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
)

@retry(
    retry=retry_if_exception_type(RETRYABLE_EXCEPTIONS),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10),
)
def resilient_api_call(endpoint: str) -> dict:
    return httpx.get(endpoint, timeout=10).json()
```

## HTTP status code retries

```python
from tenacity import retry_if_result

RETRY_STATUS_CODES = {429, 502, 503, 504}

def should_retry_response(response: httpx.Response) -> bool:
    return response.status_code in RETRY_STATUS_CODES

@retry(
    retry=retry_if_result(should_retry_response),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=10),
)
def http_request(method: str, url: str, **kwargs) -> httpx.Response:
    return httpx.request(method, url, timeout=30, **kwargs)
```

## Combined exception and status retry

```python
from tenacity import (
    retry,
    retry_if_exception_type,
    retry_if_result,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
)
import logging
import httpx

log = logging.getLogger(__name__)

TRANSIENT_EXCEPTIONS = (
    ConnectionError, TimeoutError, httpx.ConnectError, httpx.ReadTimeout,
)
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}

def is_retryable_response(response: httpx.Response) -> bool:
    return response.status_code in RETRY_STATUS_CODES

@retry(
    retry=(
        retry_if_exception_type(TRANSIENT_EXCEPTIONS)
        | retry_if_result(is_retryable_response)
    ),
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=30),
    before_sleep=before_sleep_log(log, logging.WARNING),
)
def robust_http_call(method: str, url: str, **kwargs) -> httpx.Response:
    return httpx.request(method, url, timeout=30, **kwargs)
```

## Logging retry attempts

```python
import logging
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

def log_retry_attempt(retry_state) -> None:
    exception = retry_state.outcome.exception()
    log.warning(
        "operation_retry",
        extra={
            "attempt": retry_state.attempt_number,
            "exception_type": type(exception).__name__,
            "exception_message": str(exception),
            "next_wait_seconds": (
                retry_state.next_action.sleep if retry_state.next_action else None
            ),
        },
    )

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=10),
    before_sleep=log_retry_attempt,
)
def call_with_logging(request: dict) -> dict:
    ...
```

## Timeout decorator

```python
import asyncio
from functools import wraps
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

def with_timeout(seconds: float):
    """Decorator to add timeout to async functions."""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)
        return wrapper
    return decorator

@with_timeout(30)
async def fetch_with_timeout(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.json()
```

## Stack cross-cutting concerns

```python
from functools import wraps
from collections.abc import Callable
from typing import TypeVar
from opentelemetry import trace

tracer = trace.get_tracer(__name__)
T = TypeVar("T")

def traced(name: str | None = None):
    """Open an OTel span around the call."""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        span_name = name or func.__name__

        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            with tracer.start_as_current_span(span_name) as span:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
                    raise
        return wrapper
    return decorator

# Stack: tracing → timeout → retry, business logic at the bottom
@traced("fetch_user_data")
@with_timeout(30)
@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter())
async def fetch_user_data(user_id: str) -> dict:
    ...
```

## Inject infrastructure for testability

Services take their infrastructure through the constructor. Use Pydantic for value objects, plain classes (with `Protocol` interfaces) for services.

```python
from typing import Protocol


class MetricsClient(Protocol):
    def increment(self, metric: str, tags: dict | None = None) -> None: ...
    def timing(self, metric: str, value: float) -> None: ...


class UserService:
    """Service with injected infrastructure."""

    def __init__(
        self,
        repository: UserRepository,
        metrics: MetricsClient,
    ) -> None:
        self._repo = repository
        self._metrics = metrics

    async def get_user(self, user_id: str) -> User:
        start = time.perf_counter()
        try:
            user = await self._repo.get(user_id)
            self._metrics.increment("user.fetch.success")
            return user
        except Exception:
            self._metrics.increment("user.fetch.error")
            raise
        finally:
            self._metrics.timing("user.fetch.duration", time.perf_counter() - start)


# Easy to test with fakes
service = UserService(
    repository=FakeRepository(),
    metrics=FakeMetrics(),
)
```

## Fail-safe defaults

Degrade gracefully when non-critical operations fail.

```python
from functools import wraps
from collections.abc import Callable
from typing import TypeVar
import logging

log = logging.getLogger(__name__)
T = TypeVar("T")

def fail_safe(default: T, log_failure: bool = True):
    """Return default on failure instead of raising."""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                if log_failure:
                    log.warning(
                        "fail_safe_default",
                        extra={"function": func.__name__, "error": str(e)},
                    )
                return default
        return wrapper
    return decorator

@fail_safe(default=[])
async def get_recommendations(user_id: str) -> list[str]:
    """Return empty list on failure — recommendations are non-critical."""
    ...
```

## Circuit breakers

For dependencies that fail in bursts, a circuit breaker (e.g. `purgatory`, `pybreaker`) stops the bleeding by failing fast once a failure threshold is crossed, then probing for recovery. Pair with `fail_safe` for graceful degradation downstream.

## Summary

1. **Retry only transient errors.**
2. **Exponential backoff with jitter** — `wait_exponential_jitter(initial=1, max=30)`.
3. **Cap total duration** — `stop_after_attempt(5) | stop_after_delay(60)`.
4. **Log every retry** — silent retries hide systemic problems.
5. **Decorators for cross-cutting concerns** — tracing, timeout, retry stacked.
6. **Timeouts everywhere** — every network call.
7. **Fail gracefully** — return cached/default values for non-critical paths.
8. **Inject infrastructure via Protocols** — testable services.
9. **Monitor retry rates** — high rates indicate underlying problems.
