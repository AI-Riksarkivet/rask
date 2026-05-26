# Anti-Patterns Checklist

Common Python mistakes to catch before merge. Pair with `References/design-patterns.md` (the positive side) and `References/error-handling.md` (validation/exception details).

## Contents

- Infrastructure anti-patterns
- Architecture anti-patterns
- Error-handling anti-patterns
- Resource, type-safety, and testing anti-patterns (pointers to topic refs)
- Pydantic / data-model anti-patterns
- Function-shape anti-patterns
- Logic-shape anti-patterns
- Quick review checklist
- Top gotchas

## Infrastructure anti-patterns

### Scattered timeout/retry logic

```python
# BAD: duplicated everywhere
def fetch_user(user_id):
    try:
        return requests.get(url, timeout=30)
    except Timeout:
        logger.warning("Timeout fetching user")
        return None

def fetch_orders(user_id):
    try:
        return requests.get(url, timeout=30)
    except Timeout:
        logger.warning("Timeout fetching orders")
        return None
```

**Fix:** centralize in a decorator or client wrapper. See `python-infrastructure` → `References/resilience.md`.

```python
@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter())
def http_get(url: str) -> Response:
    return httpx.get(url, timeout=30)
```

### Double retry

```python
# BAD: retrying at multiple layers
@retry(max_attempts=3)  # application retry
def call_service():
    return client.request()  # client also has retry configured!
```

**Fix:** retry at one layer only. Know your client's defaults.

### Hard-coded configuration

```python
# BAD
DB_HOST = "prod-db.example.com"
API_KEY = "sk-12345"

def connect():
    return psycopg.connect(f"host={DB_HOST}...")
```

**Fix:** typed settings via `pydantic-settings`. See `References/configuration.md`.

```python
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    db_host: str = Field(alias="DB_HOST")
    api_key: str = Field(alias="API_KEY")

settings = Settings()
```

## Architecture anti-patterns

### Exposed internal types

```python
# BAD: leaking ORM model to API
@app.get("/users/{id}")
def get_user(id: str) -> UserModel:  # SQLAlchemy model
    return db.query(UserModel).get(id)
```

**Fix:** explicit Pydantic response schemas.

```python
@app.get("/users/{id}")
def get_user(id: str) -> UserResponse:
    user = db.query(UserModel).get(id)
    return UserResponse.model_validate(user, from_attributes=True)
```

### Mixed I/O and business logic

```python
# BAD: SQL embedded in business logic
def calculate_discount(user_id: str) -> float:
    user = db.query("SELECT * FROM users WHERE id = ?", user_id)
    orders = db.query("SELECT * FROM orders WHERE user_id = ?", user_id)
    if len(orders) > 10:
        return 0.15
    return 0.0
```

**Fix:** repository pattern; keep business logic pure.

```python
def calculate_discount(user: User, orders: list[Order]) -> float:
    return 0.15 if len(orders) > 10 else 0.0
```

## Error-handling anti-patterns

### Bare exception handling

```python
# BAD: silent failure
try:
    process()
except Exception:
    pass
```

**Fix:** catch specific exceptions; log or re-raise.

```python
try:
    process()
except ConnectionError as e:
    logger.warning("Connection failed, will retry", error=str(e))
    raise
except ValueError as e:
    logger.error("Invalid input", error=str(e))
    raise BadRequestError(str(e)) from e
```

### Ignored partial failures

```python
# BAD: stops on first error
def process_batch(items):
    results = []
    for item in items:
        result = process(item)  # raises -> batch aborted
        results.append(result)
    return results
```

**Fix:** capture both successes and failures. See `References/error-handling.md`.

```python
def process_batch(items) -> BatchResult:
    succeeded: dict[int, Result] = {}
    failed: dict[int, Exception] = {}
    for idx, item in enumerate(items):
        try:
            succeeded[idx] = process(item)
        except Exception as e:
            failed[idx] = e
    return BatchResult(succeeded=succeeded, failed=failed)
```

### Missing input validation

```python
# BAD: validation deferred
def create_user(data: dict):
    return User(**data)  # crashes deep in code on bad input
```

**Fix:** validate at the boundary with Pydantic.

```python
def create_user(data: dict) -> User:
    validated = CreateUserInput.model_validate(data)
    return User.from_input(validated)
```

## Resource, type-safety, and testing anti-patterns

These have dedicated references — going there avoids two-sources-of-truth drift:

- **Resources** (unclosed files/connections, blocking calls inside `async def`): see `resource-management.md` and the "Async patterns" section of `patterns.md`. Rule of thumb: every resource through a `with` / `async with`; never `time.sleep` / `requests` inside `async def` — use `asyncio.sleep` / `httpx.AsyncClient`.
- **Type safety** (missing hints, bare `list` / `dict`): see `type-safety.md`. Rule of thumb: annotate every public signature; parameterize every container (`list[User]`, not `list`).
- **Testing** (happy-path-only, over-mocking): see `testing.md`. Rule of thumb: cover error and boundary cases (T5/T6); mock at external boundaries only — prefer real fakes/in-memory for internal collaborators.

The Quick review checklist below still tracks these for fast review without jumping files.

## Pydantic / data-model anti-patterns

### Using `@dataclass` for any new model

This project is Pydantic-only. Don't add `@dataclass` — use `BaseModel`.

```python
# BAD (this project): @dataclass
from dataclasses import dataclass

@dataclass
class Job:
    id: str
    status: str

# GOOD: Pydantic
from pydantic import BaseModel

class Job(BaseModel):
    id: str
    status: str
```

### Mutable model instances passed across boundaries

Pydantic models are mutable by default. If a value object is shared and must not change, freeze it:

```python
class Coords(BaseModel):
    model_config = {"frozen": True}

    lat: float
    lon: float
```

## Function-shape anti-patterns

### Too many arguments (F1)

More than ~3 positional arguments is a signal the function does too much, or that the arguments belong in a model. Use a Pydantic `BaseModel` for the bag.

```python
# BAD
def create_user(name, email, age, country, timezone, language, newsletter): ...

# GOOD
class NewUser(BaseModel):
    name: str
    email: EmailStr
    age: int
    country: str
    timezone: str
    language: str
    newsletter: bool

def create_user(user: NewUser) -> User: ...
```

Keyword-only arguments (`def f(*, a, b, c)`) help when 3 is genuinely needed but call sites would be hard to read positionally.

### Output / mutating arguments (F2)

Functions shouldn't mutate their parameters. Return a new value.

```python
# BAD: mutates the input
def append_footer(report: list[str]) -> None:
    report.append("---\nGenerated by System")

# GOOD: returns a new value
def with_footer(report: list[str]) -> list[str]:
    return [*report, "---\nGenerated by System"]
```

Exception: methods on a class are _expected_ to mutate `self`. The rule is about parameters owned by the caller.

### Flag arguments (F3, related to G15)

Boolean flags almost always mean the function does two things. Split it.

```python
# BAD
def render(is_test: bool) -> str:
    if is_test:
        return render_test_page()
    return render_production_page()

# GOOD — two functions, intent obvious at call site
def render_test_page() -> str: ...
def render_production_page() -> str: ...
```

If the "flag" controls _behavior shape_ (not just a runtime mode), it's worse than a flag — it's a hidden branch in a single function's responsibility.

### Dead functions / unused code (F4, G9)

If it's not called, delete it. No "just in case" code. Git remembers everything; speculative helpers don't.

```python
# Delete unused helpers when you spot them — don't preserve them
# as "might be useful later".
```

`ruff` with rule `F401` (unused imports) and `vulture` for dead code help spot these.

## Logic-shape anti-patterns

### Obscured intent (G16)

Don't be clever. Name the operation.

```python
# BAD: what does this do?
return (x & 0x0F) << 4 | (y & 0x0F)

# GOOD: name the operation
return pack_coordinates(x, y)
```

If a reader has to mentally execute the expression to know its purpose, extract a named function or constant.

### Long `if/elif` chains on type/state (G23)

A growing `if/elif` chain switching on a string/enum is a request for polymorphism — but not always with classes. In Python the lightest answer is usually a **dict dispatch**:

```python
# BAD — will grow forever, every new type needs an edit here
def calculate_pay(employee):
    if employee.type == "SALARIED":
        return employee.salary
    elif employee.type == "HOURLY":
        return employee.hours * employee.rate
    elif employee.type == "COMMISSIONED":
        return employee.base + employee.commission

# GOOD — dict of small functions, open/closed without inheritance
PAY_STRATEGIES: dict[str, Callable[[Employee], float]] = {
    "SALARIED":     lambda e: e.salary,
    "HOURLY":       lambda e: e.hours * e.rate,
    "COMMISSIONED": lambda e: e.base + e.commission,
}

def calculate_pay(employee: Employee) -> float:
    return PAY_STRATEGIES[employee.type](employee)
```

Classes only when each variant has _meaningful state and behavior_ beyond the calculation — usually they don't.

### Tangled / negative conditionals (G28, G29)

Don't make readers parse negation pyramids. Extract or invert.

```python
# BAD: negative + nested
if not user.is_inactive and not user.is_banned:
    if not (user.age < 18):
        send_newsletter(user)

# GOOD: name the positive condition
def is_eligible_for_newsletter(user: User) -> bool:
    return user.is_active and not user.is_banned and user.age >= 18

if is_eligible_for_newsletter(user):
    send_newsletter(user)
```

When a `not` would simplify the structure (`if not error_present` reads worse than `if ok`), reach for the positive name instead.

## Quick review checklist

Before finalizing code, verify:

- [ ] No scattered timeout/retry logic (centralized via decorator)
- [ ] No double retry (app + infrastructure)
- [ ] No hard-coded configuration or secrets
- [ ] No exposed internal types (ORM models, protobufs)
- [ ] No mixed I/O and business logic
- [ ] No bare `except Exception: pass`
- [ ] No ignored partial failures in batches
- [ ] No missing input validation at boundaries
- [ ] No unclosed resources (context managers everywhere)
- [ ] No blocking calls in async code
- [ ] No functions with > 3 positional args (use a Pydantic model)
- [ ] No mutating output arguments
- [ ] No boolean flag parameters (split the function)
- [ ] No dead/unused helpers
- [ ] No clever expressions whose intent isn't obvious from the name
- [ ] No long `if/elif` on type/state (use dict dispatch)
- [ ] No negative conditional pyramids
- [ ] All public functions have type hints
- [ ] Collections have type parameters
- [ ] All structured models are Pydantic (no `@dataclass`)
- [ ] Error paths are tested
- [ ] Edge cases are covered

## Top gotchas

- **Mutable default arguments share state across calls** — `def f(x=[]):` reuses the same list. Use `x: list | None = None` and create inside.
- **Bare `except:` catches `KeyboardInterrupt`** — silently absorbs Ctrl+C in long-running loops. Always be specific.
- **`is` vs `==` for cached small ints/strings** — `a is b` works for small ints (-5..256) and interned strings, fails unpredictably otherwise. Always `==` for value comparison.
- **`assert` is stripped by `python -O`** — never use for runtime validation; only for code-internal invariants.
- **`__slots__` breaks pickling, weakref, and multiple inheritance** unless declared exactly right.
