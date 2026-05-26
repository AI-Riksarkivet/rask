# Design Patterns

Fundamental design principles for maintainable Python code: KISS, single responsibility, composition over inheritance, dependency injection.

> **Defaults — read before adding any abstraction.**
> Default to **functions and modules**. Reach for **classes** only when you have state. Reach for **`Protocol`** only when there are 2+ implementations. Reach for **inheritance** almost never. Pick the simplest shape that works; revisit only when you have three concrete instances of a pattern. Java patterns transplanted directly into Python are usually wrong — see "Pattern translation" below.

## Contents

- KISS — keep it simple
- Single responsibility
- Layered architecture
- Composition over inheritance
- Rule of three
- Function size
- Dependency injection
- Guard clauses — exit early, keep the happy path flat
- SOLID in Python — what actually matters
- Law of Demeter — no train wrecks
- DRY — knowledge, not code
- Pattern translation — Java GoF → Pythonic form
- Summary
- Gotchas

## KISS — keep it simple

Before adding complexity, ask: does a simpler solution work?

```python
# Over-engineered: factory with registration
class OutputFormatterFactory:
    _formatters: dict[str, type[Formatter]] = {}

    @classmethod
    def register(cls, name: str):
        def decorator(formatter_cls):
            cls._formatters[name] = formatter_cls
            return formatter_cls
        return decorator

    @classmethod
    def create(cls, name: str) -> Formatter:
        return cls._formatters[name]()

@OutputFormatterFactory.register("json")
class JsonFormatter(Formatter): ...

# Simple: just a dict
FORMATTERS: dict[str, type[Formatter]] = {
    "json": JsonFormatter,
    "csv": CsvFormatter,
    "xml": XmlFormatter,
}

def get_formatter(name: str) -> Formatter:
    if name not in FORMATTERS:
        raise ValueError(f"Unknown format: {name}")
    return FORMATTERS[name]()
```

The factory pattern adds code without value here. Save patterns for when they solve a real problem.

## Single responsibility

Each class or function should have one reason to change.

```python
# Bad: handler does everything — HTTP parsing, validation, DB access, formatting
class UserHandler:
    async def create_user(self, request: Request) -> Response:
        data = await request.json()
        if not data.get("email"):
            return Response({"error": "email required"}, status=400)
        user = await db.execute(
            "INSERT INTO users (email, name) VALUES ($1, $2) RETURNING *",
            data["email"], data["name"],
        )
        return Response({"id": user.id, "email": user.email}, status=201)

# Good: separated concerns
class UserService:
    """Business logic only."""
    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo

    async def create_user(self, data: CreateUserInput) -> User:
        user = User(email=data.email, name=data.name)
        return await self._repo.save(user)

class UserHandler:
    """HTTP concerns only."""
    def __init__(self, service: UserService) -> None:
        self._service = service

    async def create_user(self, request: Request) -> Response:
        data = CreateUserInput.model_validate(await request.json())
        user = await self._service.create_user(data)
        return Response(user.model_dump(), status=201)
```

HTTP changes don't affect business logic, and vice versa.

## Layered architecture

```
┌─────────────────────────────────────┐
│ API Layer (handlers)                │
│ - Parse requests                    │
│ - Call services                     │
│ - Format responses                  │
└─────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────┐
│ Service Layer (business logic)      │
│ - Domain rules and validation       │
│ - Orchestrate operations            │
│ - Pure functions where possible     │
└─────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────┐
│ Repository Layer (data access)      │
│ - SQL queries                       │
│ - External API calls                │
│ - Cache operations                  │
└─────────────────────────────────────┘
```

Each layer depends only on the layers below it.

```python
# Repository: data access
class UserRepository:
    async def get_by_id(self, user_id: str) -> User | None:
        row = await self._db.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return User(**row) if row else None

# Service: business logic
class UserService:
    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo

    async def get_user(self, user_id: str) -> User:
        user = await self._repo.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError(user_id)
        return user

# Handler: HTTP concerns
@app.get("/users/{user_id}")
async def get_user(user_id: str) -> UserResponse:
    user = await user_service.get_user(user_id)
    return UserResponse.model_validate(user, from_attributes=True)
```

## Composition over inheritance

Build behavior by combining objects rather than inheriting.

```python
# Inheritance: rigid, hard to test
class EmailNotificationService(NotificationService):
    def __init__(self):
        super().__init__()
        self._smtp = SmtpClient()  # hard to mock

    def notify(self, user: User, message: str) -> None:
        self._smtp.send(user.email, message)

# Composition: flexible, testable
class NotificationService:
    def __init__(
        self,
        email_sender: EmailSender,
        sms_sender: SmsSender | None = None,
        push_sender: PushSender | None = None,
    ) -> None:
        self._email = email_sender
        self._sms = sms_sender
        self._push = push_sender

    async def notify(
        self,
        user: User,
        message: str,
        channels: set[str] | None = None,
    ) -> None:
        channels = channels or {"email"}
        if "email" in channels:
            await self._email.send(user.email, message)
        if "sms" in channels and self._sms and user.phone:
            await self._sms.send(user.phone, message)
        if "push" in channels and self._push and user.device_token:
            await self._push.send(user.device_token, message)

# Test with fakes
service = NotificationService(
    email_sender=FakeEmailSender(),
    sms_sender=FakeSmsSender(),
)
```

**When you DO subclass** (rare — usually for ABCs, framework hooks, or Pydantic model extension), mark overrides with `@override` (PEP 698). `ty` will flag broken overrides when the parent renames a method.

```python
from typing import override

class BaseRepository[T]:
    async def get(self, id: str) -> T | None: ...

class UserRepository(BaseRepository[User]):
    @override
    async def get(self, id: str) -> User | None:
        ...
```

## Rule of three

Wait until you have three instances before abstracting. Duplication is often better than the wrong abstraction.

Two similar functions? Look carefully — they may not actually be the same shape. Different validation, different processing, different error semantics. Only after a third case should you consider extracting.

## Function size

Extract when a function:

- Exceeds 20–50 lines (varies by complexity)
- Serves multiple distinct purposes
- Has deeply nested logic (3+ levels)

```python
# Too long, multiple concerns mixed
def process_order(order: Order) -> Result:
    # 50 lines of validation...
    # 30 lines of inventory check...
    # 40 lines of payment processing...
    # 20 lines of notification...
    ...

# Composed from focused functions
def process_order(order: Order) -> Result:
    """Process a customer order through the complete workflow."""
    validate_order(order)
    reserve_inventory(order)
    payment_result = charge_payment(order)
    send_confirmation(order, payment_result)
    return Result(success=True, order_id=order.id)
```

## Dependency injection

Pass dependencies through constructors for testability. Use `Protocol` for the contracts.

```python
from typing import Protocol

class Logger(Protocol):
    def info(self, msg: str, **kwargs) -> None: ...
    def error(self, msg: str, **kwargs) -> None: ...

class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int) -> None: ...

class UserService:
    def __init__(
        self,
        repository: UserRepository,
        cache: Cache,
        logger: Logger,
    ) -> None:
        self._repo = repository
        self._cache = cache
        self._logger = logger

    async def get_user(self, user_id: str) -> User:
        cached = await self._cache.get(f"user:{user_id}")
        if cached:
            self._logger.info("Cache hit", user_id=user_id)
            return User.model_validate_json(cached)

        user = await self._repo.get_by_id(user_id)
        if user:
            await self._cache.set(f"user:{user_id}", user.model_dump_json(), ttl=300)
        return user

# Production
service = UserService(
    repository=PostgresUserRepository(db),
    cache=RedisCache(redis),
    logger=otel_logger(),
)

# Testing
service = UserService(
    repository=InMemoryUserRepository(),
    cache=FakeCache(),
    logger=NullLogger(),
)
```

In FastAPI, do the same via `Depends` + `Annotated`. See the `fastapi` skill.

## Guard clauses — exit early, keep the happy path flat

The most common readability win in a function with branching: flip the conditions and `return` at the top. Don't nest the success path inside `if`s.

```python
# Bad: pyramid of doom
def process(data):
    if data is not None:
        if isinstance(data, dict):
            if "value" in data:
                return data["value"] * 2
    return None

# Good: guard clauses, flat happy path
def process(data: dict | None) -> int | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        return None
    if "value" not in data:
        return None
    return data["value"] * 2
```

```python
# Bad
def dashboard(request):
    if request.user.is_authenticated:
        if request.user.is_staff:
            return render(request, "dashboard.html")
    return redirect("login")

# Good
def dashboard(request):
    if not request.user.is_authenticated:
        return redirect("login")
    if not request.user.is_staff:
        return HttpResponseForbidden()
    return render(request, "dashboard.html")
```

**When NOT to use them.** A few guard clauses at the top of a function are great. If exits are scattered through the body and you have to scroll to track all of them, the function is doing too much — split it. Many guard clauses is a smell that the function should become several.

## SOLID in Python — what actually matters

SOLID was written for Java. Naively bolting all five onto Python produces Java-in-Python: ABC hierarchies for things that should be functions, `IUserRepository` interfaces with one implementation, DI containers for a 200-line script. **Don't.** Here's what Python keeps and what it drops:

| Principle                     | Python verdict                                                                                                                                                                  |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **S** — Single Responsibility | **Keep.** Always useful. Already covered above.                                                                                                                                 |
| **O** — Open/Closed           | **Reframe.** In Python, "open for extension" means "add a new function and put it in a dict / Protocol", not "make a new subclass". Don't build inheritance hierarchies for it. |
| **L** — Liskov Substitution   | **Barely applies.** Python uses duck typing — if it quacks, it's a duck. Liskov shows up only when you actually subclass, which you should rarely do.                           |
| **I** — Interface Segregation | **Use `Protocol`, sparingly.** Define a `Protocol` only when there are 2+ implementations and you want to type-check them. One-implementation interfaces are pure ceremony.     |
| **D** — Dependency Inversion  | **Keep, but lightweight.** Pass dependencies through `__init__` (or FastAPI `Depends`). No containers, no factories. Constructor + `Protocol` is enough.                        |

**Default to functions and modules. Reach for classes when you have state. Reach for `Protocol` when you have multiple implementations. Reach for inheritance almost never.** Composition + dict dispatch + module-level functions cover 90% of what SOLID-in-Java tries to solve with class hierarchies.

If your "SOLID refactor" added an ABC, a factory, a DI container, and three interfaces for one concrete implementation — undo it. The original code was probably fine.

## Law of Demeter — no train wrecks

A function should only talk to its immediate collaborators. `a.b.c.d.do_something()` couples the caller to four classes' internal structure; any of them changing breaks you.

```python
# Bad: train wreck — caller knows about Options and ScratchDir internals
output_dir = context.options.scratch_dir.absolute_path

# Good: ask the collaborator
output_dir = context.get_scratch_dir()
```

```python
# Bad
def discount(user) -> float:
    if user.account.subscription.tier.name == "premium":
        return 0.15
    return 0.0

# Good — the question lives where the data lives
def discount(user) -> float:
    return 0.15 if user.is_premium() else 0.0
```

**Pydantic exception.** Reaching into `model.field.subfield` on a Pydantic value object is fine — it's data, not behavior. Demeter is about not chaining through _behavior_ boundaries (services, repositories, sessions).

## DRY — knowledge, not code

DRY is about **eliminating duplicated knowledge** (the same business rule expressed in multiple places), not eliminating code that _looks_ similar.

```python
# Same business rule duplicated — UNIFY
# in signup
if len(password) < 8 or not has_uppercase(password):
    raise ValueError("Password too weak")
# in password-reset
if len(new_password) < 8 or not has_uppercase(new_password):
    raise ValueError("Password too weak")

# Extract: one source of truth for "what makes a valid password"
def validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if not any(c.isupper() for c in password):
        raise ValueError("Password must contain an uppercase letter")
```

```python
# Looks similar but different concepts — DO NOT unify
def shipping_cost(weight: float, distance: float) -> float:
    return weight * 0.5 + distance * 0.1

def insurance_premium(value: float, risk_factor: float) -> float:
    return value * 0.5 + risk_factor * 0.1
# These will diverge as the business evolves. The shape collision is coincidence.
```

**The wrong DRY is worse than duplication.** A shared function taking five parameters and three boolean flags to serve every caller is harder to follow and harder to change than the original duplication. When in doubt, follow the Rule of Three above and wait for the third occurrence before extracting.

## Pattern translation — Java GoF → Pythonic form

When asked to "implement pattern X in Python", default to the Python form. The Java-shape implementation is almost always over-engineered here.

| Pattern                      | Don't (Java-in-Python)                                | Do (Python form)                                                                                                   |
| ---------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| **Singleton**                | `__new__` + `threading.Lock` + double-checked locking | A module-level instance: `settings = Settings()` in `config.py`. Modules are imported once.                        |
| **Factory / Factory Method** | `ABC` + concrete factory classes + `register()`       | A dict mapping key → class or callable: `FORMATTERS: dict[str, type[Formatter]] = {...}`.                          |
| **Builder**                  | Fluent `.method().url().header().build()` chains      | Keyword arguments on a Pydantic model: `HttpRequest(method=..., url=..., headers=...)`.                            |
| **Strategy**                 | `ABC` + N strategy subclasses                         | A dict of functions: `PAY_STRATEGIES: dict[str, Callable[[Employee], float]] = {...}`. See `anti-patterns.md` G23. |
| **Observer / Pub-sub**       | `Observable` / `Observer` / `update()` ABCs           | A callback list, `asyncio.Queue`, or a NATS JetStream subject (cross-process).                                     |
| **Adapter**                  | Heavy ABC hierarchy wrapping two SDKs                 | A `Protocol` plus two concrete classes implementing it. See "Composition over inheritance".                        |
| **Decorator**                | Wrapper class with `__call__` and inheritance         | A function decorator: `@retry`, `@traced`. See `python-infrastructure/References/resilience.md`.                   |
| **Template Method**          | ABC with abstract steps overridden by subclasses      | Pass a function (or `Protocol`) as a parameter.                                                                    |
| **Command**                  | `Command` class with `execute()` method               | A callable (function or closure).                                                                                  |
| **Iterator**                 | `__iter__` / `__next__` boilerplate                   | A generator function (`def gen(): yield ...`) or comprehension.                                                    |
| **Chain of Responsibility**  | Linked-list of `Handler` subclasses                   | A list of functions in a loop, or `functools.reduce`.                                                              |

If you're asked to implement a named pattern and the Python form isn't in this table, the answer is almost always "use a function or a `Protocol`, not a class hierarchy".

## Summary

1. **Keep it simple** — simplest thing that works.
2. **Single responsibility** — one reason to change per unit.
3. **Layered architecture** — handlers → services → repositories.
4. **Compose, don't inherit** — combine objects via constructor injection.
5. **Rule of three** — wait before abstracting.
6. **Small focused functions** — 20–50 lines, one purpose.
7. **Guard clauses** — exit early, keep the happy path flat.
8. **SOLID lightly** — keep S and D, reframe O, mostly skip L and I.
9. **Law of Demeter** — one dot through behavior boundaries.
10. **DRY knowledge, not code** — same rule in 2+ places means unify; same shape, different concept means leave alone.
11. **Inject dependencies via Protocols** — for testability.
12. **Delete before abstracting** — remove dead code first.
13. **Explicit over clever** — readable beats elegant.

## Gotchas

- **Singleton via `__new__` doesn't work cleanly with subclasses** — each subclass gets its own instance. Use a module-level instance or `__init_subclass__` if needed.
- **Observer pattern with strong references leaks memory** when subscribers go out of scope but the publisher holds them. Use `weakref.WeakSet` for subscriber storage.
- **`functools.lru_cache` on instance methods retains `self` forever** (via the cache holding the bound method) — apply to module-level functions, or use `methodtools.lru_cache`.
- **ABCs: `@abstractmethod` enforced at instantiation, not subclass definition** — a subclass missing the method passes type-checking until you try to construct it.
