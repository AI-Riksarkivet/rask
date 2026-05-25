# Resource Management

Release resources deterministically with context managers. Connections, file handles, sockets, and transactions must close reliably — even on exceptions.

## Contents

- Class-based context manager
- Async context manager
- `@contextmanager` for simple cases
- Selective exception suppression
- Streaming with accumulated state (Pydantic)
- Efficient string accumulation
- Stream metrics
- Multiple resources with `ExitStack`
- Summary
- Gotchas

## Class-based context manager

```python
from types import TracebackType

class DatabaseConnection:
    """Database connection with automatic cleanup."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Connection | None = None

    def connect(self) -> None:
        self._conn = psycopg.connect(self._dsn)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "DatabaseConnection":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


with DatabaseConnection(dsn) as db:
    result = db.execute(query)
```

## Async context manager

```python
class AsyncDatabasePool:
    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def __aenter__(self) -> "AsyncDatabasePool":
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min_size, max_size=self._max_size,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def execute(self, query: str, *args) -> list[dict]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)


async with AsyncDatabasePool(dsn) as pool:
    users = await pool.execute("SELECT * FROM users WHERE active = $1", True)
```

## `@contextmanager` for simple cases

```python
from contextlib import contextmanager, asynccontextmanager
import time
import logging

log = logging.getLogger(__name__)

@contextmanager
def timed_block(name: str):
    """Time a block of code."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        log.info("block_completed", extra={"name": name, "duration_s": round(elapsed, 3)})

with timed_block("data_processing"):
    process_large_dataset()


@asynccontextmanager
async def database_transaction(conn: AsyncConnection):
    """Manage database transaction with auto-commit/rollback."""
    await conn.execute("BEGIN")
    try:
        yield conn
        await conn.execute("COMMIT")
    except Exception:
        await conn.execute("ROLLBACK")
        raise


async with database_transaction(conn) as tx:
    await tx.execute("INSERT INTO users ...")
    await tx.execute("INSERT INTO audit_log ...")
```

## Selective exception suppression

Only suppress specific, documented exceptions. Default `__exit__` returns `None` (falsy) which propagates everything.

```python
class StreamWriter:
    """Writer that handles broken pipe gracefully."""

    def __init__(self, stream) -> None:
        self._stream = stream

    def __enter__(self) -> "StreamWriter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        self._stream.close()
        # Suppress BrokenPipeError (client disconnected) — expected, not an error
        return exc_type is BrokenPipeError
```

## Streaming with accumulated state (Pydantic)

Maintain incremental chunks plus an accumulated buffer during a streaming operation.

```python
from collections.abc import Generator
from pydantic import BaseModel, Field, PrivateAttr

class StreamingResult(BaseModel):
    """Accumulated streaming result."""

    chunks: list[str] = Field(default_factory=list)
    _finalized: bool = PrivateAttr(default=False)

    @property
    def content(self) -> str:
        return "".join(self.chunks)

    def add_chunk(self, chunk: str) -> None:
        if self._finalized:
            raise RuntimeError("Cannot add to finalized result")
        self.chunks.append(chunk)

    def finalize(self) -> str:
        self._finalized = True
        return self.content


def stream_with_accumulation(
    response: StreamingResponse,
) -> Generator[tuple[str, str], None, str]:
    """Stream response while accumulating content.

    Yields:
        (accumulated_content, new_chunk) for each chunk.

    Returns:
        Final accumulated content.
    """
    result = StreamingResult()
    for chunk in response.iter_content():
        result.add_chunk(chunk)
        yield result.content, chunk
    return result.finalize()
```

## Efficient string accumulation

```python
def accumulate_stream(stream) -> str:
    # BAD: O(n²) due to string immutability
    # content = ""
    # for chunk in stream:
    #     content += chunk

    # GOOD: O(n) with list and join
    chunks: list[str] = []
    for chunk in stream:
        chunks.append(chunk)
    return "".join(chunks)
```

## Stream metrics

```python
import time
from collections.abc import Generator
from pydantic import BaseModel

class StreamMetrics(BaseModel):
    time_to_first_byte_ms: float
    total_time_ms: float
    chunk_count: int
    total_bytes: int

def stream_with_metrics(
    response: StreamingResponse,
) -> Generator[str, None, StreamMetrics]:
    start = time.perf_counter()
    first_chunk_time: float | None = None
    chunk_count = 0
    total_bytes = 0

    for chunk in response.iter_content():
        if first_chunk_time is None:
            first_chunk_time = time.perf_counter() - start
        chunk_count += 1
        total_bytes += len(chunk.encode())
        yield chunk

    total_time = time.perf_counter() - start
    return StreamMetrics(
        time_to_first_byte_ms=round((first_chunk_time or 0) * 1000, 2),
        total_time_ms=round(total_time * 1000, 2),
        chunk_count=chunk_count,
        total_bytes=total_bytes,
    )
```

## Multiple resources with `ExitStack`

```python
from contextlib import ExitStack, AsyncExitStack
from pathlib import Path

def process_files(paths: list[Path]) -> list[str]:
    results = []
    with ExitStack() as stack:
        files = [stack.enter_context(open(p)) for p in paths]
        for f in files:
            results.append(f.read())
    return results

async def process_connections(hosts: list[str]) -> list[dict]:
    results = []
    async with AsyncExitStack() as stack:
        connections = [
            await stack.enter_async_context(connect_to_host(host))
            for host in hosts
        ]
        for conn in connections:
            results.append(await conn.fetch_data())
    return results
```

## Summary

1. **Always use context managers** for resources needing cleanup.
2. **Clean up unconditionally** — `__exit__` runs even on exception.
3. **Don't suppress exceptions unexpectedly** — return `False`/`None` unless suppression is intentional and documented.
4. **`@contextmanager`** for simple patterns; class-based for stateful ones.
5. **`ExitStack`** for a dynamic number of resources.
6. **List + join**, not string concatenation, for accumulation.
7. **Document exception suppression behavior** in the docstring.
8. **Test cleanup paths** — verify resources are released on errors.

## Gotchas

- **`with open()` inside a generator: file closes when the generator is GC'd, not at end of iteration** — fine in CPython (refcount), risky in PyPy.
- **`ExitStack`: `enter_context` for the `__enter__`/`__exit__` protocol; `push` for cleanup-only callbacks** — mixing them with wrong ordering can swallow exceptions in the cleanup chain.
- **Async context managers MUST use `async with`** — `with` on an async manager silently returns a coroutine; no cleanup runs.
- **`tempfile.NamedTemporaryFile` on Windows can't be reopened while open** — `delete=False` + explicit cleanup is the cross-platform path.
- **`contextlib.suppress(BaseException)` swallows `KeyboardInterrupt`** — always pass concrete `Exception` subclasses.
