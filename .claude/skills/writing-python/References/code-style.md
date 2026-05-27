# Code Style & Documentation

Consistent style and clear documentation make codebases maintainable. This project uses `ruff` for lint+format and `ty` for type checking — both fast Astral tools, both replace older alternatives.

## Contents

- Toolchain configuration
- Naming conventions (PEP 8, length matches scope, side effects, leading underscore = private)
- Import organization
- Google-style docstrings
- Comments — write few, write well (C1/C3/C5)
- Line length and formatting
- Summary

## Toolchain configuration

```toml
# pyproject.toml
[tool.ruff]
line-length = 120
target-version = "py314"

[tool.ruff.lint]
select = [
    "E",    # pycodestyle errors
    "W",    # pycodestyle warnings
    "F",    # pyflakes
    "I",    # isort
    "B",    # flake8-bugbear
    "C4",   # flake8-comprehensions
    "UP",   # pyupgrade
    "SIM",  # flake8-simplify
]
ignore = ["E501"]  # line length handled by formatter

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.ty.terminal]
error-on-warning = true
```

Run:

```bash
uvx ruff check --fix .
uvx ruff format .
uvx ty check
```

`uvx` runs the tool in an isolated environment without installing into your project's venv — faster and keeps the venv clean. For tools that need your project (`pytest`, `fastapi`), use `uv run` instead.

## Naming conventions

PEP 8 with clarity over brevity.

**Files and modules** — descriptive `snake_case`, never abbreviate:

```python
# Good
user_repository.py
order_processing.py

# Avoid
usr_repo.py
ord_proc.py
```

**Classes** — `PascalCase`; acronyms stay uppercase:

```python
class UserRepository: ...
class HTTPClientFactory: ...
```

**Functions and variables** — `snake_case`:

```python
def get_user_by_email(email: str) -> User | None:
    retry_count = 3
```

**Module-level constants** — `SCREAMING_SNAKE_CASE`:

```python
MAX_RETRY_ATTEMPTS = 3
DEFAULT_TIMEOUT_SECONDS = 30
API_BASE_URL = "https://api.example.com"
```

**Name length matches scope (N5).** Short names are fine inside tight scopes; module-level identifiers need to read on their own.

```python
# Fine — tight scope
total = sum(n for n in numbers)
for i, item in enumerate(items): ...

# Fine — module-level constant, descriptive
MAX_RETRY_ATTEMPTS_BEFORE_FAILURE = 5

# Bad — cryptic at module level
MAX = 5
d = 86400
```

Loop indices `i, j, k`, common abbreviations `db`, `cfg`, `ctx`, single-letter coords `x, y` — all fine in small scopes. Reach the module/class boundary and the bar goes up.

**Names describe side effects (N7).** If a function does something beyond what its name suggests, the name is misleading.

```python
# BAD — sounds read-only, secretly writes a file
def get_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text("{}")
    return json.loads(CONFIG_PATH.read_text())

# GOOD — name reveals the write
def get_or_create_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text("{}")
    return json.loads(CONFIG_PATH.read_text())
```

`get_` / `load_` / `read_` should be pure. `create_`, `save_`, `update_`, `delete_`, `_or_create` signal mutation. Be honest about which one you're writing.

**Leading underscore = private (PEP 8).** A single leading underscore on a function, method, attribute, or module-level name marks it as **internal — not part of the public API**. Callers outside the module/class shouldn't import or touch it; it can change or disappear without a deprecation cycle.

```python
class UserService:
    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo               # private attribute — internal state

    async def get_user(self, user_id: str) -> User:   # public — part of the API
        return await self._fetch(user_id)

    async def _fetch(self, user_id: str) -> User:     # private helper — implementation detail
        ...

# Module-level helpers used only inside this file
def _normalize_email(email: str) -> str: ...

# Re-exports / public API of the module
__all__ = ["UserService"]
```

Rules of thumb:

- Internal helpers, intermediate state, "implementation details" — prefix with `_`.
- Anything callers are _allowed_ to depend on — no underscore.
- `__double_leading_underscore` triggers name mangling on classes (`obj._ClassName__foo`). You almost never want this — use single `_` instead.
- `__dunder__` names are reserved for Python protocols (`__init__`, `__enter__`, `__iter__`, etc.). Don't invent your own.
- `_unused` is also the convention for "I'm receiving this but ignoring it" — e.g. `for _ in range(n):` or `_, value = pair`.

When `ty` / `ruff` see something marked private being used across module boundaries, treat that as a smell — either the thing should be public (drop the underscore) or the caller shouldn't be reaching in.

## Import organization

Stdlib → third-party → local, each group separated by a blank line. Absolute imports only.

```python
# Stdlib
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

# Third-party
import httpx
from pydantic import BaseModel
from sqlalchemy import Column

# Local
from myproject.models import User
from myproject.services import UserService

# Type-checking-only imports (no runtime cost; breaks circular imports)
if TYPE_CHECKING:
    from myproject.heavy_module import HeavyClass
```

Avoid relative imports (`from ..utils import x`) — absolute paths are easier to grep and survive moves.

`if TYPE_CHECKING:` is the standard way to import symbols you only need in annotations — the block is skipped at runtime, so it costs nothing and lets two modules reference each other in types without circular-import errors. Use string annotations (`"HeavyClass"`) or `from __future__ import annotations` to keep the references valid at runtime.

## Google-style docstrings

Every public class, method, and function gets a docstring.

**Simple function:**

```python
def get_user(user_id: str) -> User:
    """Retrieve a user by their unique identifier."""
    ...
```

**Complex function:**

```python
def process_batch(
    items: list[Item],
    max_workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
) -> BatchResult:
    """Process items concurrently using a worker pool.

    Args:
        items: The items to process. Must not be empty.
        max_workers: Maximum concurrent workers. Defaults to 4.
        on_progress: Optional callback receiving (completed, total) counts.

    Returns:
        BatchResult containing succeeded items and any failures with
        their associated exceptions.

    Raises:
        ValueError: If items is empty.
        ProcessingError: If the batch cannot be processed.

    Example:
        >>> result = process_batch(items, max_workers=8)
        >>> print(f"Processed {len(result.succeeded)} items")
    """
    ...
```

**Class:**

```python
class UserService:
    """Service for managing user operations.

    Attributes:
        repository: Data access layer for user persistence.
        logger: Logger instance for operation tracking.
    """

    def __init__(self, repository: UserRepository, logger: Logger) -> None:
        self.repository = repository
        self.logger = logger
```

## Comments — write few, write well

**The default is no comment.** Self-documenting code — a precise function name, a tight signature, type hints, well-named locals — replaces 90% of comments. A comment is a sign that the code itself isn't clear enough; the fix is usually to rename or refactor, not to annotate.

**Why this matters here:** LLM-generated Python tends to be over-commented — every function gets a docstring restating its name, every loop gets a `# loop through items`, every assignment gets a `# set x to value`. That's noise. It increases context cost, decays as the code changes, and trains future readers to ignore comments (because most are useless). Be ruthless when reviewing: if a comment restates the code, delete it.

Reach for a comment only when:

- The _why_ is non-obvious — a hidden constraint, subtle invariant, workaround for a specific bug, or behavior that would surprise a careful reader.
- A docstring on a public API explains _what callers need to know_ (args, return shape, raised exceptions, edge cases) — not what the implementation does.

**No metadata in comments (C1).** Git owns authorship, dates, tickets, and change history. Comments don't.

```python
# BAD
# Author: alice
# Created: 2024-03-12
# Ticket: PROJ-1234
def calculate_tax(amount: float) -> float: ...

# GOOD — git blame answers all of this
def calculate_tax(amount: float) -> float: ...
```

**No redundant comments (C3).** Don't restate what the code obviously does.

```python
# BAD
i += 1  # increment i
user.save()  # save the user

# GOOD — only when the *why* isn't obvious
i += 1  # skip the header row; payload starts at index 1
```

**No commented-out code (C5).** Delete it. Git remembers everything.

```python
# DELETE THIS — it's an abomination
# def old_calculate_tax(income):
#     return income * 0.15
```

**Keep docstrings honest (C2).** When code changes, update or delete the docstring. A docstring that lies about behavior is worse than no docstring.

**Comment rules of thumb:**

- One short line is almost always enough.
- Don't write multi-paragraph docstrings on private helpers.
- Don't reference the current task, fix, or callers (`# added for the X flow`, `# used by Y`) — those belong in the PR description and rot.
- TODOs are allowed when actionable: `# TODO(<github-username>): refactor after the v2 rollout`. Aimless TODOs (`# TODO: improve later`) get deleted.

## Line length and formatting

120 characters. `ruff format` handles the rest — don't argue with the formatter.

```python
def create_user(
    email: str,
    name: str,
    role: UserRole = UserRole.MEMBER,
    notify: bool = True,
) -> User:
    ...

# Chain method calls clearly
result = (
    db.query(User)
    .filter(User.active == True)
    .order_by(User.created_at.desc())
    .limit(10)
    .all()
)

# Concatenate long strings
error_message = (
    f"Failed to process user {user_id}: "
    f"received status {response.status_code} "
    f"with body {response.text[:100]}"
)
```

## Summary

1. **`ruff` for lint + format**, **`ty` for type checks** — no other style tools.
2. **120-char lines.**
3. **Descriptive names**, no abbreviations; **length matches scope** (short in tight loops, long for module-level).
4. **Names describe side effects** — `get_` is pure, `create_`/`save_` is not.
5. **Leading underscore (`_foo`) = private** — internal helpers, attributes, and module-level names that callers shouldn't touch.
6. **Absolute imports**, grouped stdlib/third-party/local.
7. **Google-style docstrings** on every public API.
8. **Few comments, none redundant**, no metadata, no commented-out code.
9. **Automate in CI** — `uvx ruff check`, `uvx ruff format --check`, `uvx ty check` on every commit.
