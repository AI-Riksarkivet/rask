---
name: writing-python
description: Idiomatic Python 3.14+ — language style, type safety, design patterns, anti-patterns, error handling, resource management, configuration, CLI, and testing. Pydantic-first (no dataclasses). Use when writing or reviewing Python code, scaffolding services or CLI tools, designing validation/exception strategies, externalizing config, or establishing project conventions. NOT FOR system-reliability concerns like background jobs, retries, or observability (use `python-infrastructure`).
allowed-tools: Read, Bash, Grep, Glob
---

# Writing Python (3.14+)

Language-level Python for this project: stdlib first, Pydantic for any structured data, `uv`/`ruff`/`ty` for tooling, type hints everywhere.

## Scope routing

| If you need to…                                                            | Read                                |
| -------------------------------------------------------------------------- | ----------------------------------- |
| Configure ruff/ty, naming, imports, docstrings (Google style)              | `References/code-style.md`          |
| Add type annotations, generics, protocols, narrowing                       | `References/type-safety.md`         |
| Decide between composition vs inheritance, layer code, inject dependencies | `References/design-patterns.md`     |
| Audit code against a checklist of known bad patterns                       | `References/anti-patterns.md`       |
| Validate inputs, design exception hierarchies, handle partial failures     | `References/error-handling.md`      |
| Manage connections/file handles/streams via context managers               | `References/resource-management.md` |
| Load env vars, set up `pydantic-settings`, validate config at boot         | `References/configuration.md`       |
| Look up everyday patterns (project layout, async, logging, pathlib)        | `References/patterns.md`            |
| Build a CLI (`typer`, output formats, progress, exit codes)                | `References/cli.md`                 |
| Write tests with `pytest` (fixtures, async, parametrize, coverage)         | `References/testing.md`             |

## House style — non-negotiable

- **Pydantic, not dataclasses.** Every structured value object is a `BaseModel`. Settings are `pydantic-settings.BaseSettings`. Don't reach for `@dataclass`.
- **Type hints on every public signature.** `ty` runs in CI; untyped code fails.
- **`uv` for everything Python-level.** Don't use `pip`/`poetry`/`pyenv` directly.
- **`ruff` for lint + format.** Don't add `black`/`isort`/`flake8`.
- **`ty` for type checking.** Don't add `mypy`/`pyright`. CI runs `uvx ty check`.
- **Stdlib first.** Only add a dep when stdlib genuinely doesn't fit (e.g. `httpx` for async HTTP, `pydantic` for validation).
- **Explicit over clever.** Readable beats elegant.
- **Fail fast.** Validate at boundaries; raise informative errors immediately.
- **Self-documenting code, not noise comments.** Reach for a comment when the _why_ is non-obvious (workaround, constraint, surprising invariant). When you'd write one to explain _what_ the code does, ask first whether a clearer name or a small extracted function would read better — usually it does, sometimes the inline code is fine and a split would just add noise. Judgment call. Never restate what the code already says. See `References/code-style.md`.

## Quick patterns

```python
# Type hints — all signatures, modern union syntax
def get_user(user_id: str) -> User | None: ...
async def fetch(url: str, timeout: float = 30.0) -> dict[str, object]: ...

# Pydantic model (instead of @dataclass)
from pydantic import BaseModel, Field

class Config(BaseModel):
    host: str
    port: int = Field(default=8080, ge=1, le=65535)
    tags: list[str] = Field(default_factory=list)

# Pattern matching for shape dispatch
match event:
    case {"type": "click", "x": x, "y": y}:
        handle_click(x, y)
    case {"type": "key", "code": code}:
        handle_key(code)
    case _:
        raise ValueError(f"Unknown event: {event}")
```

## Toolchain

```bash
uv sync                    # install deps from uv.lock
uv add <pkg>               # add a runtime dep
uv add --dev <pkg>         # add a dev dep

uvx ruff check --fix .     # lint + autofix (isolated env, no install needed)
uvx ruff format .          # format
uvx ty check               # type-check

uv run pytest -v           # tests (project-aware — pytest must import your package)
uv run pytest --cov=src    # tests + coverage
uv run fastapi dev         # FastAPI app, when applicable
```

**Rule of thumb**: `uvx <tool>` for tools that only need the file (`ruff`, `ty`); `uv run <tool>` for tools that need your project's environment (`pytest`, `fastapi`, anything that imports your code).

## Python 3.14 features worth knowing

- **Template strings** — `t"Hello {name}"` returns a `Template` object (use for SQL/HTML where literal interpolation is unsafe).
- **`except` without parens** — `except ValueError, TypeError:` is valid.
- **`concurrent.interpreters`** — true parallelism via subinterpreters.
- **`compression.zstd`** — Zstandard in stdlib.
- **Free-threaded build (PEP 703)** — no GIL, opt-in.

## Cross-skill boundaries

- **`python-infrastructure`** — what to do when the function above needs to retry across a network boundary, run in a worker, or be observable in prod.
- **`fastapi`** — HTTP routing, dependency injection, FastAPI/Pydantic conventions.
- **`astral:uv`** / **`astral:ruff`** / **`astral:ty`** — deep tool reference for the toolchain.

## Top gotchas

- **`async def` vs `def` for FastAPI dependencies**: async deps run on the event loop; sync deps run in a thread pool. Mixing without thought causes blocking or extra context-switch overhead.
- **Mutable default arguments share state** — `def f(x=[]):` reuses the same list across calls. Use `x: list | None = None` and create inside.
- **`assert` is stripped by `python -O`** — never use for runtime validation; only for code-internal invariants.
- **Pydantic API surface to remember** — `model_dump` / `model_validate` / `model_dump_json` / `model_validate_json`, `Field(alias=..., default_factory=...)`, `model_config = {...}`, `@field_validator` / `@model_validator`. Don't paste snippets using `dict()`, `parse_obj()`, `Config` class, or `@validator` — those are from an older API and won't work.
- **`functools.lru_cache` on instance methods retains `self` forever** — apply to module-level functions, or use `methodtools.lru_cache`.
