# Error Handling

Validate inputs at boundaries, raise meaningful exceptions, preserve context, and never let one bad item abort a batch.

## Contents

- Early input validation
- Convert to domain types at boundaries
- Pydantic for complex validation
- Map errors to standard exception types
- Custom exceptions with structured context
- Exception chaining — preserve the cause
- Batch processing with partial failures
- Progress reporting for long operations
- Summary
- Gotchas

## Early input validation

Validate before any expensive work.

```python
def process_order(
    order_id: str,
    quantity: int,
    discount_percent: float,
) -> OrderResult:
    if not order_id:
        raise ValueError("'order_id' is required")
    if quantity <= 0:
        raise ValueError(f"'quantity' must be positive, got {quantity}")
    if not 0 <= discount_percent <= 100:
        raise ValueError(f"'discount_percent' must be 0-100, got {discount_percent}")

    return _process_validated_order(order_id, quantity, discount_percent)
```

## Convert to domain types at boundaries

Parse strings/external data into typed domain objects as early as possible.

```python
from enum import Enum

class OutputFormat(str, Enum):
    JSON = "json"
    CSV = "csv"
    PARQUET = "parquet"

def parse_output_format(value: str) -> OutputFormat:
    """Parse string to OutputFormat enum.

    Raises:
        ValueError: If format is not recognized.
    """
    try:
        return OutputFormat(value.lower())
    except ValueError:
        valid = ", ".join(f.value for f in OutputFormat)
        raise ValueError(f"Invalid format '{value}'. Valid options: {valid}") from None

def export_data(data: list[dict], format_str: str) -> bytes:
    output_format = parse_output_format(format_str)  # fail fast
    # rest of function uses typed OutputFormat
    ...
```

## Pydantic for complex validation

For anything more than a few inline checks, use a Pydantic model.

```python
from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator

class CreateUserInput(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=100)
    age: int = Field(ge=0, le=150)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        return v.strip().title()

try:
    user_input = CreateUserInput.model_validate({
        "email": "user@example.com",
        "name": "john doe",
        "age": 25,
    })
except ValidationError as e:
    # Pydantic gives structured errors with .errors()
    for err in e.errors():
        log.error("validation_error", field=err["loc"], msg=err["msg"])
    raise
```

## Map errors to standard exception types

| Failure type        | Exception           | Example                  |
| ------------------- | ------------------- | ------------------------ |
| Invalid input       | `ValueError`        | Bad parameter values     |
| Wrong type          | `TypeError`         | Expected string, got int |
| Missing item        | `KeyError`          | Dict key not found       |
| Operational failure | `RuntimeError`      | Service unavailable      |
| Timeout             | `TimeoutError`      | Operation took too long  |
| File not found      | `FileNotFoundError` | Path doesn't exist       |
| Permission denied   | `PermissionError`   | Access forbidden         |

```python
# Good: specific exception with context
raise ValueError(f"'page_size' must be 1-100, got {page_size}")

# Bad: generic, no context
raise Exception("Invalid parameter")
```

## Custom exceptions with structured context

```python
class ApiError(Exception):
    """Base exception for API errors."""

    def __init__(
        self,
        message: str,
        status_code: int,
        response_body: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message)

class RateLimitError(ApiError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded. Retry after {retry_after}s",
            status_code=429,
        )

def handle_response(response: Response) -> dict:
    match response.status_code:
        case 200:
            return response.json()
        case 401:
            raise ApiError("Invalid credentials", 401)
        case 404:
            raise ApiError(f"Resource not found: {response.url}", 404)
        case 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            raise RateLimitError(retry_after)
        case code if 400 <= code < 500:
            raise ApiError(f"Client error: {response.text}", code)
        case code if code >= 500:
            raise ApiError(f"Server error: {response.text}", code)
```

## Exception chaining — preserve the cause

```python
import httpx

class ServiceError(Exception):
    """High-level service operation failed."""

def upload_file(path: str) -> str:
    try:
        with open(path, "rb") as f:
            response = httpx.post("https://upload.example.com", files={"file": f})
            response.raise_for_status()
            return response.json()["url"]
    except FileNotFoundError as e:
        raise ServiceError(f"Upload failed: file not found at '{path}'") from e
    except httpx.HTTPStatusError as e:
        raise ServiceError(
            f"Upload failed: server returned {e.response.status_code}"
        ) from e
    except httpx.RequestError as e:
        raise ServiceError("Upload failed: network error") from e
```

Use `from e` to keep the original exception in `__cause__`; `from None` suppresses the cause chain (use when the lower exception is noise).

## Batch processing with partial failures

Track successes and failures separately. Pydantic generics make `BatchResult[T]` typed.

```python
from pydantic import BaseModel, Field

class BatchResult[T](BaseModel):
    """Results from batch processing."""

    succeeded: dict[int, T] = Field(default_factory=dict)  # index -> result
    failed: dict[int, str] = Field(default_factory=dict)   # index -> error message

    model_config = {"arbitrary_types_allowed": True}

    @property
    def success_count(self) -> int:
        return len(self.succeeded)

    @property
    def failure_count(self) -> int:
        return len(self.failed)

    @property
    def all_succeeded(self) -> bool:
        return not self.failed


def process_batch(items: list[Item]) -> BatchResult[ProcessedItem]:
    """Process items, capturing individual failures.

    Returns:
        BatchResult with succeeded items and failure messages by index.
    """
    succeeded: dict[int, ProcessedItem] = {}
    failed: dict[int, str] = {}

    for idx, item in enumerate(items):
        try:
            succeeded[idx] = process_single_item(item)
        except Exception as e:
            failed[idx] = f"{type(e).__name__}: {e}"

    return BatchResult(succeeded=succeeded, failed=failed)


result = process_batch(items)
if not result.all_succeeded:
    log.warning(
        "batch_partial_failure",
        failure_count=result.failure_count,
        failed_indices=list(result.failed),
    )
```

Storing failure as `str` (vs the raw `Exception`) keeps `BatchResult` JSON-serializable. If you need the exception object, store both: `dict[int, ExceptionInfo]` with a small `ExceptionInfo` Pydantic model.

## Progress reporting for long operations

```python
from collections.abc import Callable

ProgressCallback = Callable[[int, int, str], None]  # current, total, status

def process_large_batch(
    items: list[Item],
    on_progress: ProgressCallback | None = None,
) -> BatchResult[ProcessedItem]:
    total = len(items)
    succeeded: dict[int, ProcessedItem] = {}
    failed: dict[int, str] = {}

    for idx, item in enumerate(items):
        if on_progress:
            on_progress(idx, total, f"Processing {item.id}")
        try:
            succeeded[idx] = process_single_item(item)
        except Exception as e:
            failed[idx] = f"{type(e).__name__}: {e}"

    if on_progress:
        on_progress(total, total, "Complete")

    return BatchResult(succeeded=succeeded, failed=failed)
```

## Summary

1. **Validate early** — at boundaries, before expensive work.
2. **Use specific exceptions** — `ValueError`, `TypeError`, not bare `Exception`.
3. **Include context** — what, why, how to fix.
4. **Convert types at boundaries** — parse strings to enums/Pydantic models early.
5. **Chain exceptions** — `raise X from e` to preserve debug info.
6. **Handle partial failures** — `BatchResult[T]` for batches.
7. **Use Pydantic** for any structured validation.
8. **Document failure modes** in docstrings.
9. **Test error paths** — happy-path-only tests miss the important bugs.

## Gotchas

- **`raise X from None` suppresses the cause chain; `raise X` preserves `__context__`** — different stack traces. Use `from None` when the lower exception is noise.
- **Bare `raise` inside `except` re-raises the original; `raise e` rebuilds the traceback** — only bare `raise` preserves the original location.
- **`assert` is stripped by `python -O`** — runtime validation MUST use explicit `if + raise`.
- **Exception groups need `except*`, not `except`** — `except ExceptionGroup` catches but doesn't unwrap; `except*` does selective sub-exception handling.
- **`finally` runs even after `return`** and overrides the return value if it has its own `return` — accidentally returning from `finally` swallows exceptions.
