"""Shared test isolation for the viewer test package.

`viewer.main.create_app()` calls `load_dotenv()`, so without isolation a
developer's local (gitignored) `.env` leaks into the tests. In particular
`RASK_API_PREFIX` — set to `/api` in dev `.env`s that run the viewer behind the
gateway — would remount every route under `/api/*` and 404 the tests' hardcoded
`/api/v1/...` paths. The per-test `client` fixtures already pin the other
`.env`-sourced settings they depend on (input/output, DB, Ray URL); this pins
the API prefix the same way so the suite is hermetic regardless of local `.env`.

`load_dotenv()` does not override variables already present in the environment,
so setting it here wins over whatever `.env` contains.
"""

import pytest


@pytest.fixture(autouse=True)
def _pin_api_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin `RASK_API_PREFIX` to the documented default so tests don't depend on `.env`."""
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
