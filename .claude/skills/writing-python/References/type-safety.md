# Type Safety

Annotations are enforced documentation. This project runs `ty check` in CI; untyped code fails.

## Contents

- Annotate every public signature
- Union syntax
- Type narrowing with guards
- Generics (PEP 695 syntax)
- Generic repository pattern
- Bounded type parameters (Pydantic-aware)
- Protocols for structural typing
- `Self` return type (PEP 673)
- Type aliases (`type` statement)
- Callable types
- Typed decorators (`**P`, `R`)
- ty configuration
- Summary

## Annotate every public signature

```python
def get_user(user_id: str) -> User: ...

def process_batch(
    items: list[Item],
    max_workers: int = 4,
) -> BatchResult[ProcessedItem]: ...

class UserRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def find_by_id(self, user_id: str) -> User | None: ...
    async def save(self, user: User) -> User: ...
```

## Union syntax

```python
def find_user(user_id: str) -> User | None: ...
def parse_value(v: str) -> int | float | str: ...
```

`Optional[T]` and `Union[A, B]` from `typing` are legacy. Don't use them.

## Type narrowing with guards

```python
def process_user(user_id: str) -> UserData:
    user = find_user(user_id)
    if user is None:
        raise UserNotFoundError(f"User {user_id} not found")
    # Type checker knows `user` is User here, not User | None
    return UserData(name=user.name, email=user.email)

def process_items(items: list[Item | None]) -> list[ProcessedItem]:
    valid_items = [item for item in items if item is not None]
    # valid_items: list[Item]
    return [process(item) for item in valid_items]
```

## Generics

**Why generics?** They let you write a class or function once and reuse it for many element types **without losing type information**. `list[int]` and `list[str]` aren't compatible — `ty` will catch passing one where the other is expected. Without generics, you'd either lose the element type (`list` → "list of anything") or duplicate the class per element type.

A generic also keeps the _relationship_ between inputs and outputs intact. In `Result[T, E]` below, the type checker knows that `.unwrap()` on a `Result[Config, ConfigError]` returns a `Config` — not "some value of unknown type". That's the whole point.

Use PEP 695 type-parameter syntax — it's the form in this project. The `[T, E: Exception]` declares two type parameters; `E: Exception` is a _bound_ meaning "any subclass of `Exception`".

```python
class Result[T, E: Exception]:
    """Either a success value or an error."""

    def __init__(
        self,
        value: T | None = None,
        error: E | None = None,
    ) -> None:
        if (value is None) == (error is None):
            raise ValueError("Exactly one of value or error must be set")
        self._value = value
        self._error = error

    @property
    def is_success(self) -> bool:
        return self._error is None

    def unwrap(self) -> T:
        if self._error is not None:
            raise self._error
        return self._value  # type: ignore[return-value]


def parse_config(path: str) -> Result[Config, ConfigError]:
    try:
        return Result(value=Config.from_file(path))
    except ConfigError as e:
        return Result(error=e)
```

`TypeVar` from `typing` still works but is legacy — don't reach for it in new code.

## Generic repository pattern

```python
from abc import ABC, abstractmethod

class Repository[T, ID](ABC):
    @abstractmethod
    async def get(self, id: ID) -> T | None: ...

    @abstractmethod
    async def save(self, entity: T) -> T: ...

    @abstractmethod
    async def delete(self, id: ID) -> bool: ...

class UserRepository(Repository[User, str]):
    async def get(self, id: str) -> User | None:
        row = await self._db.fetchrow("SELECT * FROM users WHERE id = $1", id)
        return User(**row) if row else None

    async def save(self, entity: User) -> User: ...
    async def delete(self, id: str) -> bool: ...
```

## Bounded type parameters (Pydantic-aware)

```python
from pydantic import BaseModel

def validate_and_create[ModelT: BaseModel](model_cls: type[ModelT], data: dict) -> ModelT:
    """Create a validated Pydantic model from dict."""
    return model_cls.model_validate(data)

class User(BaseModel):
    name: str
    email: str

user = validate_and_create(User, {"name": "Alice", "email": "a@b.com"})
# user: User
```

## Protocols for structural typing

**Why protocols?** They let you say "anything with these methods, regardless of inheritance". Python's duck typing already works that way at runtime; `Protocol` makes it visible to `ty` so the type checker can verify it. Use a `Protocol` when:

- You have two or more concrete classes that play the same role (e.g. `RedisCache` and `InMemoryCache` both satisfy `Cache`).
- A function takes a collaborator and shouldn't care about its concrete type (e.g. `def serialize(obj: Serializable) -> str:`).
- You want a clean seam for testing — production passes a real implementation, tests pass a fake — without forcing both to inherit a common base.

If there's only ever **one** implementation, don't bother with a `Protocol`. The concrete class IS the interface.

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class Serializable(Protocol):
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> "Serializable": ...

def serialize(obj: Serializable) -> str:
    return json.dumps(obj.to_dict())
```

Common reusable protocols:

```python
class Closeable(Protocol):
    def close(self) -> None: ...

class AsyncCloseable(Protocol):
    async def close(self) -> None: ...

class HasId(Protocol):
    @property
    def id(self) -> str: ...
```

## `Self` return type (PEP 673)

For methods that return the instance (fluent APIs, `model_copy`-style helpers, factory methods on a base class), use `Self` instead of quoting the class name. `Self` resolves correctly in subclasses; the string form does not.

```python
from typing import Self
from pydantic import BaseModel

class QueryBuilder(BaseModel):
    table: str
    conditions: list[str] = []

    def where(self, cond: str) -> Self:
        return self.model_copy(update={"conditions": [*self.conditions, cond]})

    def limit(self, n: int) -> Self:
        # Returns the correct subclass type if this is subclassed
        ...
```

Don't write `-> "QueryBuilder"` for the return type — `Self` is more accurate and avoids the string-annotation gotcha.

## Type aliases

```python
# Simple
type UserId = str
type UserDict = dict[str, Any]

# Generic
type Handler[T] = Callable[[Request], T]
type AsyncHandler[T] = Callable[[Request], Awaitable[T]]
```

`TypeAlias` from `typing` is legacy. Use the `type` statement.

## Callable types

```python
from collections.abc import Callable, Awaitable

ProgressCallback = Callable[[int, int], None]  # (current, total)
AsyncHandler = Callable[[Request], Awaitable[Response]]

# With keyword args via Protocol
class OnProgress(Protocol):
    def __call__(self, current: int, total: int, *, message: str = "") -> None: ...
```

## Typed decorators (`**P`, `R`)

**Why this matters.** An untyped decorator silently destroys its wrapped function's type information — call sites see `Any`, autocomplete dies, `ty` stops catching wrong arguments. The `**P` / `R` pattern preserves the wrapped function's full signature so callers get the same type-checking they'd get without the decorator.

`**P` (a _parameter spec_) stands in for "whatever parameters the wrapped function takes". `R` stands in for "whatever it returns". The decorator passes them through unchanged, and `ty` does the rest.

Reach for this whenever you write a decorator. Otherwise it lies about types.

```python
from collections.abc import Callable
from functools import wraps
import time
import logging

log = logging.getLogger(__name__)

def timed[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Log how long the wrapped function takes."""
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        start = time.perf_counter()
        result = func(*args, **kwargs)
        log.info("op_timed", extra={"op": func.__name__, "ms": (time.perf_counter() - start) * 1000})
        return result
    return wrapper


@timed
def calculate(numbers: list[int], multiplier: int = 1) -> int:
    return sum(numbers) * multiplier


# Fully typed at the call site
result: int = calculate([1, 2, 3], multiplier=2)
```

When a decorator **injects an extra argument** that callers shouldn't pass, use `Concatenate`:

```python
from collections.abc import Callable
from typing import Concatenate
from functools import wraps

class DbSession: ...

def with_db[**P, R](func: Callable[Concatenate[DbSession, P], R]) -> Callable[P, R]:
    """Inject a DbSession as the first argument."""
    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        session = open_session()
        return func(session, *args, **kwargs)
    return wrapper


@with_db
def fetch_users(session: DbSession, limit: int = 10) -> list[User]:
    ...


# Caller doesn't supply the session
users = fetch_users(limit=5)
```

`ParamSpec` / `Concatenate` from `typing` still work but the PEP 695 `**P` form is cleaner. The legacy form is fine to read; don't write it in new code.

## ty configuration

```toml
# pyproject.toml
[tool.ty.terminal]
error-on-warning = true
```

For incremental adoption on legacy code, use per-module overrides — see the `astral:ty` skill.

## Summary

1. Annotate **all public APIs** — params, returns, class attributes.
2. Use **`T | None`** for optional types.
3. **Run `ty check`** in CI.
4. **Generics** via PEP 695 syntax (`class Foo[T]`, `def f[T: Bound](...)`, `def deco[**P, R](...)`). No `TypeVar` / `ParamSpec`.
5. **Protocols** for structural typing — interfaces without inheritance.
6. **Narrow with guards** (`if x is None: raise ...`) to help the checker.
7. **Type aliases** via the `type` statement.
8. **Minimize `Any`** — acceptable only for truly dynamic data or untyped third-party code.
