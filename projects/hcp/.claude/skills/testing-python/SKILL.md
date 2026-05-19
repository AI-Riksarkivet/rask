---
name: testing-python
description: Write and evaluate effective Python tests using pytest. Use when writing tests, reviewing test code, debugging test failures, or improving test coverage. Covers test design, fixtures, parameterization, mocking, and async testing.
---

# Writing Effective Python Tests

## Core Principles

Every test should be **atomic**, **self-contained**, and test **single functionality**. A test that tests multiple things is harder to debug and maintain.

## Test Structure

### Atomic unit tests

Each test should verify a single behavior. The test name should tell you what's broken when it fails. Multiple assertions are fine when they all verify the same behavior.

```python
# Good: Name tells you what's broken
def test_user_creation_sets_defaults():
    user = User(name="Alice")
    assert user.role == "member"
    assert user.id is not None
    assert user.created_at is not None

# Bad: If this fails, what behavior is broken?
def test_user():
    user = User(name="Alice")
    assert user.role == "member"
    user.promote()
    assert user.role == "admin"
    assert user.can_delete_others()
```

### Use parameterization for variations of the same concept

```python
import pytest

@pytest.mark.parametrize("input,expected", [
    ("hello", "HELLO"),
    ("World", "WORLD"),
    ("", ""),
    ("123", "123"),
])
def test_uppercase_conversion(input, expected):
    assert input.upper() == expected
```

### Use separate tests for different functionality

Don't parameterize unrelated behaviors. If the test logic differs, write separate tests.

## Project-Specific Rules

### No async markers needed

This project uses `asyncio_mode = "auto"` globally. Write async tests without decorators:

```python
# Correct
async def test_async_operation():
    result = await some_async_function()
    assert result == expected

# Wrong - don't add this
@pytest.mark.asyncio
async def test_async_operation():
    ...
```

### Imports at module level

Put ALL imports at the top of the file:

```python
# Correct
import pytest
from fastmcp import FastMCP
from fastmcp.client import Client

async def test_something():
    mcp = FastMCP("test")
    ...

# Wrong - no local imports
async def test_something():
    from fastmcp import FastMCP  # Don't do this
    ...
```

### Use in-memory transport for testing

Pass FastMCP servers directly to clients:

```python
from fastmcp import FastMCP
from fastmcp.client import Client

mcp = FastMCP("TestServer")

@mcp.tool
def greet(name: str) -> str:
    return f"Hello, {name}!"

async def test_greet_tool():
    async with Client(mcp) as client:
        result = await client.call_tool("greet", {"name": "World"})
        assert result[0].text == "Hello, World!"
```

Only use HTTP transport when explicitly testing network features.

### Inline snapshots for complex data

Use `inline-snapshot` for testing JSON schemas and complex structures:

```python
from inline_snapshot import snapshot

def test_schema_generation():
    schema = generate_schema(MyModel)
    assert schema == snapshot()  # Will auto-populate on first run
```

Commands:
- `pytest --inline-snapshot=create` - populate empty snapshots
- `pytest --inline-snapshot=fix` - update after intentional changes

## Fixtures

### Prefer function-scoped fixtures

```python
@pytest.fixture
def client():
    return Client()

async def test_with_client(client):
    result = await client.ping()
    assert result is not None
```

### Use `tmp_path` for file operations

```python
def test_file_writing(tmp_path):
    file = tmp_path / "test.txt"
    file.write_text("content")
    assert file.read_text() == "content"
```

## Mocking

### Use respx for httpx HTTP mocking

For services that use `httpx` (e.g. MAPI clients), use **respx** to intercept HTTP calls at the transport level. This is preferred over `unittest.mock.AsyncMock` because it tests the real httpx request/response flow.

**Decorator pattern** — for unit-testing a service in isolation:

```python
import httpx
import respx

HCP_BASE = "https://my-api.example.com:9090/mapi"

@respx.mock
async def test_service_sends_correct_headers(service):
    route = respx.get(f"{HCP_BASE}/tenants").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    resp = await service.get("/tenants")

    assert resp.status_code == 200
    assert route.called
    request = route.calls.last.request
    assert "authorization" in request.headers
```

**Fixture pattern** — for endpoint/integration tests with FastAPI + ASGITransport:

```python
import respx
import pytest

@pytest.fixture
def hcp_mock():
    """respx mock context — non-matching requests pass through to ASGITransport."""
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as mock:
        yield mock

async def test_list_tenants(client, auth_headers, hcp_mock):
    hcp_mock.get(f"{HCP_BASE}/tenants").mock(
        return_value=httpx.Response(200, json={"name": ["t1"]})
    )
    resp = await client.get("/api/v1/mapi/tenants", headers=auth_headers)
    assert resp.status_code == 200
```

Key points:
- `assert_all_mocked=False` lets test client ASGITransport requests pass through
- `assert_all_called=False` avoids errors for unused routes
- URL matching ignores query parameters by default
- Use `route.calls.last.request` to inspect what was sent

### Use MagicMock for boto3/S3 services

boto3 does not use httpx, so respx cannot intercept it. Use `unittest.mock.MagicMock` for S3 service mocking:

```python
from unittest.mock import MagicMock

@pytest.fixture
def mock_s3_service():
    mock = MagicMock(spec=S3Service)
    mock.list_buckets.return_value = {"Buckets": []}
    return mock
```

### Mock at the boundary

```python
from unittest.mock import patch, AsyncMock

async def test_external_api_call():
    with patch("mymodule.external_client.fetch", new_callable=AsyncMock) as mock:
        mock.return_value = {"data": "test"}
        result = await my_function()
        assert result == {"data": "test"}
```

### Don't mock what you own

Test your code with real implementations when possible. Mock external services, not internal classes.

## Test Naming

Use descriptive names that explain the scenario:

```python
# Good
def test_login_fails_with_invalid_password():
def test_user_can_update_own_profile():
def test_admin_can_delete_any_user():

# Bad
def test_login():
def test_update():
def test_delete():
```

## Error Testing

```python
import pytest

def test_raises_on_invalid_input():
    with pytest.raises(ValueError, match="must be positive"):
        calculate(-1)

async def test_async_raises():
    with pytest.raises(ConnectionError):
        await connect_to_invalid_host()
```

## Running Tests

```bash
uv run pytest -n auto              # Run all tests in parallel
uv run pytest -n auto -x           # Stop on first failure
uv run pytest path/to/test.py      # Run specific file
uv run pytest -k "test_name"       # Run tests matching pattern
uv run pytest -m "not integration" # Exclude integration tests
```

## Checklist

Before submitting tests:
- [ ] Each test tests one thing
- [ ] No `@pytest.mark.asyncio` decorators
- [ ] Imports at module level
- [ ] Descriptive test names
- [ ] Using in-memory transport (not HTTP) unless testing networking
- [ ] Parameterization for variations of same behavior
- [ ] Separate tests for different behaviors
