# Testing

Tests use `pytest` + `pytest-asyncio` + `pytest-cov`. Coverage gate in CI; aim for 100% on new modules unless there's a written exception.

## Contents

- Install
- Basic tests
- Parametrize
- Fixtures
- Mocking
- Mocking HTTPX with `respx`
- Async tests
- Test organization
- conftest.py
- Integration tests
- Coverage
- F.I.R.S.T. principles
- Test boundary conditions (T5)
- Exhaustively test near bugs (T6)
- One concept per test
- `@pytest.mark.skip` is a question, not an answer
- Guidelines

## Install

```bash
uv add --dev pytest pytest-asyncio pytest-cov
```

```bash
uv run pytest -v
uv run pytest --cov=src
uv run pytest --cov=src --cov-fail-under=80
```

`uv run pytest` (not `uvx pytest`): pytest needs to import your package, so it has to run inside the project's venv, not an isolated tool environment.

## Basic tests

```python
import pytest

def test_validate_email_valid():
    assert validate_email("user@example.com") is None

def test_validate_email_empty():
    with pytest.raises(ValidationError, match="email required"):
        validate_email("")

def test_validate_email_invalid():
    with pytest.raises(ValidationError, match="invalid format"):
        validate_email("invalid")
```

## Parametrize

```python
@pytest.mark.parametrize(
    "email,expected_error",
    [
        ("user@example.com", None),
        ("", "email required"),
        ("invalid", "invalid format"),
        ("user@", "invalid format"),
    ],
)
def test_validate_email(email: str, expected_error: str | None):
    if expected_error:
        with pytest.raises(ValidationError, match=expected_error):
            validate_email(email)
    else:
        assert validate_email(email) is None
```

## Fixtures

```python
from unittest.mock import Mock

@pytest.fixture
def mock_repo():
    return Mock(spec=UserRepository)

@pytest.fixture
def user_service(mock_repo):
    return UserService(repo=mock_repo)

def test_get_user(user_service, mock_repo):
    mock_repo.get.return_value = User(id="123", name="Test")

    result = user_service.get_user("123")

    assert result.name == "Test"
    mock_repo.get.assert_called_once_with("123")
```

## Mocking

```python
from unittest.mock import Mock, patch

def test_with_mock():
    mock_client = Mock()
    mock_client.fetch.return_value = {"status": "ok"}

    service = Service(client=mock_client)
    result = service.process()

    assert result == "ok"
    mock_client.fetch.assert_called_once()

@patch("mypackage.services.external_api")
def test_with_patch(mock_api):
    mock_api.call.return_value = {"data": "test"}

    result = process_data()

    assert result == "test"
```

## Mocking HTTPX with `respx`

`unittest.mock` is wrong for HTTPX — patching `httpx.AsyncClient.get` skips the request lifecycle (URL building, headers, retries) and fails to catch bugs that surface only on the wire. Use **`respx`** instead — it intercepts at the transport layer, so your code goes through the real HTTPX request pipeline up to the point of sending.

```bash
uv add --dev respx
```

### The `respx_mock` fixture (default)

```python
import httpx
import pytest


@pytest.mark.respx(base_url="https://api.example.com")
async def test_get_user(respx_mock):
    respx_mock.get("/users/42").mock(return_value=httpx.Response(200, json={"id": 42, "name": "Ada"}))

    async with httpx.AsyncClient(base_url="https://api.example.com") as client:
        r = await client.get("/users/42")

    assert r.json() == {"id": 42, "name": "Ada"}
    assert respx_mock["/users/42"].called  # if named, or use route var
```

`@pytest.mark.respx(...)` accepts `base_url=`, `assert_all_called=True/False`, `assert_all_mocked=True/False`. By default both asserts are on — every routed call must fire, and every unrouted call raises. That's usually what you want; flip `assert_all_mocked=False` for tests that only want to mock a subset.

### Side effects — callable / exception / iterable

```python
# 1. Function side-effect — inspect the request, return a dynamic Response
def _create_user(request: httpx.Request) -> httpx.Response:
    payload = httpx.Request.read(request)
    return httpx.Response(201, json={"id": 1, **payload})

respx_mock.post("/users").mock(side_effect=_create_user)

# 2. Exception side-effect — simulate a transport failure
respx_mock.get("/flaky").mock(side_effect=httpx.ConnectError)

# 3. Iterable side-effect — different response per call (for retry tests)
respx_mock.get("/eventually-ok").mock(side_effect=[
    httpx.Response(503),
    httpx.Response(503),
    httpx.Response(200, json={"ok": True}),
])
```

### Assertions via `route.calls`

```python
route = respx_mock.post("/orders").mock(return_value=httpx.Response(201))
# ... exercise code that calls the API ...
assert route.call_count == 1
sent = route.calls.last.request
assert sent.headers["Authorization"] == "Bearer test-token"
assert sent.read() == b'{"item_id": 42}'
```

### Without patching — `httpx.MockTransport`

For unit tests that construct their own `httpx.Client` (not relying on a global), skip the patching machinery entirely:

```python
import respx

router = respx.Router()
router.get("https://api.example.com/health").respond(200)

def test_with_transport():
    transport = httpx.MockTransport(router.handler)
    with httpx.Client(transport=transport) as client:
        assert client.get("https://api.example.com/health").status_code == 200
    router.assert_all_called()  # explicit — no auto post-check in this style
```

### Rules

- **Never `@patch("module.httpx_client.get")`** — patches a method, misses the request pipeline. Use `respx` so the code under test goes through real HTTPX up to the transport.
- **Tight URL matching.** Prefer `respx_mock.get("/users/42")` over loose regexes — a route matching too widely hides bugs where the wrong URL gets built.
- **One route per outbound call site.** If your service hits three endpoints, register three routes. Don't combine into one regex catch-all.
- **For FastAPI route tests** that call external HTTPX clients, combine `respx_mock` with `app.dependency_overrides` for the `HttpDep`. See `fastapi/references/dependencies.md` § Overriding dependencies in tests.

## Async tests

```python
import pytest

@pytest.mark.asyncio
async def test_async_fetch():
    result = await fetch_data("https://api.example.com")
    assert result is not None

@pytest.fixture
async def async_client():
    client = AsyncClient()
    yield client
    await client.close()

@pytest.mark.asyncio
async def test_with_async_fixture(async_client):
    result = await async_client.get("/users")
    assert result.status == 200
```

In `pyproject.toml`, enable async mode globally:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

## Test organization

```
tests/
├── conftest.py          # Shared fixtures
├── test_domain/
│   └── test_user.py
├── test_services/
│   └── test_user_service.py
└── test_integration/
    └── test_api.py
```

## conftest.py

```python
import pytest

@pytest.fixture
def sample_user() -> User:
    return User(id="123", name="Test", email="test@example.com")

@pytest.fixture
def db_session():
    session = create_test_session()
    yield session
    session.rollback()
    session.close()
```

## Integration tests

```python
@pytest.mark.integration
def test_database_integration(db_session):
    repo = UserRepository(db_session)

    user = User(name="Test", email="test@example.com")
    repo.save(user)

    result = repo.get(user.id)
    assert result.name == "Test"
```

```bash
uv run pytest -m integration
uv run pytest -m "not integration"
```

## Coverage

```bash
uv run pytest --cov=src --cov-report=html
uv run pytest --cov=src --cov-fail-under=80
```

## F.I.R.S.T. principles

Every unit test should be:

- **Fast** — under 100 ms. Slow tests get skipped; skipped tests rot. Hit real DBs/queues only in integration tests.
- **Independent** — no order dependencies. A test that fails when run alone but passes after another test is a bug in the test.
- **Repeatable** — same result every time, any environment. No clocks, no network, no random seeds without pinning.
- **Self-validating** — `pytest` exits 0 or non-zero. Never "look at the output" tests.
- **Timely** — written with the code, not weeks after. If you can't write the test, you don't understand the requirement yet.

## Test boundary conditions (T5)

Bugs cluster at boundaries — empty inputs, single elements, pagination edges, off-by-one. Test them explicitly, by name.

```python
def test_pagination_boundaries():
    items = list(range(100))

    # First page
    assert paginate(items, page=1, size=10) == items[0:10]

    # Last page
    assert paginate(items, page=10, size=10) == items[90:100]

    # Beyond last page
    assert paginate(items, page=11, size=10) == []

    # Page zero is invalid
    with pytest.raises(ValueError):
        paginate(items, page=0, size=10)

    # Empty input
    assert paginate([], page=1, size=10) == []
```

The boundary checklist worth running through for any function on a sequence/range:

- Empty input
- Single element
- Exactly the limit
- One over the limit
- Negative / zero where positive is expected
- `None` where a value is expected (and vice versa)

## Exhaustively test near bugs (T6)

When you find a bug, write tests for **every similar case**, not just the one that fired. Bugs cluster — if you missed a leap-year check, you probably also missed month-length edges.

```python
# Found bug: off-by-one in last_day_of_month for February in a leap year.
# Don't just fix and add one test. Cover every month.
def test_last_day_of_month():
    assert last_day_of_month(2024,  1) == 31
    assert last_day_of_month(2024,  2) == 29  # leap-year Feb
    assert last_day_of_month(2023,  2) == 28  # non-leap Feb
    assert last_day_of_month(2000,  2) == 29  # century leap (divisible by 400)
    assert last_day_of_month(1900,  2) == 28  # century non-leap
    assert last_day_of_month(2024,  4) == 30
    assert last_day_of_month(2024, 12) == 31
```

The bug fix lasts because the surrounding cases are now proven, not just the one that broke prod.

## One concept per test

Don't bundle assertions about different concepts into one test. When it fails, the message should tell you exactly what broke.

```python
# BAD — one test, four concepts; the failure message tells you nothing
def test_user():
    user = User(name="Alice", email="alice@example.com")
    assert user.name == "Alice"
    assert user.email == "alice@example.com"
    assert user.is_valid()
    user.activate()
    assert user.is_active

# GOOD — one concept each, parametrize where shape repeats
def test_user_stores_name():
    assert User(name="Alice", email="alice@example.com").name == "Alice"

def test_user_stores_email():
    assert User(name="Alice", email="alice@example.com").email == "alice@example.com"

def test_new_user_is_valid():
    assert User(name="Alice", email="alice@example.com").is_valid()

def test_user_can_be_activated():
    user = User(name="Alice", email="alice@example.com")
    user.activate()
    assert user.is_active
```

Use `pytest.mark.parametrize` when you'd otherwise duplicate the body across cases — that's still "one concept", parameterized.

## `@pytest.mark.skip` is a question, not an answer

A skipped test marks an ambiguity ("is this still valid?"). Either fix it or delete it. If you genuinely have to skip, write _why_ and what unblocks it.

```python
# BAD
@pytest.mark.skip(reason="flaky, fix later")
def test_async_operation(): ...

# OK — has a concrete unblock condition
@pytest.mark.skip(reason="Requires the staging Redis; see CONTRIBUTING.md")
def test_cache_invalidation(): ...
```

`@pytest.mark.xfail` is for tests that _currently_ fail in a known way and should start passing when the fix lands. Add a `strict=True` so an unexpected pass becomes a failure — that's how you find out the bug got fixed.

## Guidelines

- One concept per test (parametrize when shape repeats).
- Descriptive test names — `test_<unit>_<scenario>_<expected_result>`.
- Use fixtures for setup, not module-level state.
- Keep tests independent — no order dependencies.
- Test behavior, not implementation details.
- Cover error paths AND boundary conditions, not just happy paths.
- Mock at the boundary — prefer real DBs/queues for integration tests.
- Fast (<100 ms) by default; integration tests live behind `pytest -m integration`.
- Skipped tests need a written unblock condition or get deleted.
