"""Shared test isolation for the core service test package.

`core.main.create_app()` calls `load_dotenv()`, so without isolation a
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
    """Pin `RASK_API_PREFIX` + clear the new S3-endpoint aliases so tests don't depend on `.env`.

    The S3 endpoint now resolves from RASK_S3_ENDPOINT_URL / S3_ENDPOINT_URL / HCP_ENDPOINT
    (AliasChoices, canonical-first). Tests that force S3 unconfigured clear HCP_ENDPOINT
    per-test; clear the two newer-named aliases here so a local `.env` setting either of
    them can't leak an endpoint in and flip those assertions. Tests that configure S3 set
    HCP_ENDPOINT (a different var), so this is conflict-free.
    """
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
    monkeypatch.delenv("RASK_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
