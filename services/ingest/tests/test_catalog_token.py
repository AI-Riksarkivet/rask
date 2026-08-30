"""The catalog bearer comes from the SECRET STORE, and from nowhere else.

The estate's rule is that the Dapr secret store (OpenBao) is the SOLE source for a credential,
fail-closed, with no env fallback — because an optional secret store is the "wired but never read"
state an audit already found once, where services took secrets from plaintext pod env and the
integration was decorative.

This plane broke that rule quietly, and the cost was measured in-cluster rather than argued:
`CatalogServiceClient` read `os.getenv("RASK_CATALOG_TOKEN")`, which on a governed estate is UNSET
(the chart ships no plaintext env for a credential — the rule working). So every catalog call went
out with no `Authorization` header and the catalog answered

    401 {"type": ".../unauthenticatederror", "detail": "Missing bearer token"}

on `describe_table`. That call is inside `ensure_dataset`, the FIRST activity of every run, so no
ingest could create its bronze dataset: every run ended FAILED with `units 0/0` and nothing said why
until the pod log was read. The door was right; the caller had no way to be authenticated.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """`catalog_token` is `lru_cache`d — a token fetch per HTTP call would put the secret store on the
    hot path of every unit — so each test must start from a cold cache or it reads the previous one."""
    from ingest import catalog_service

    catalog_service.catalog_token.cache_clear()


def test_the_token_comes_from_the_SECRET_STORE(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest import catalog_service

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "true")
    monkeypatch.setattr(
        "service_kit.governed.secrets.fetch_required_secrets",
        lambda store, key, *, require: {require: "from-the-store"},
    )

    assert catalog_service.catalog_token() == "from-the-store"


def test_there_is_NO_ENV_FALLBACK(monkeypatch: pytest.MonkeyPatch) -> None:
    """The heart of the rule. A fallback makes the store optional, and an optional store is one nobody
    notices has stopped working — the credential keeps coming from env and the "governed" claim is a
    label. So an env var must not be able to satisfy this even when the store is unavailable."""
    from ingest import catalog_service

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "true")
    monkeypatch.setenv("RASK_CATALOG_TOKEN", "smuggled-in-through-env")

    def _refuse(store: str, key: str, *, require: str) -> dict[str, str]:
        raise RuntimeError(f"secret {require!r} unavailable from Dapr store {store!r}/{key!r} — failing closed")

    monkeypatch.setattr("service_kit.governed.secrets.fetch_required_secrets", _refuse)

    with pytest.raises(RuntimeError, match="failing closed"):
        catalog_service.catalog_token()


def test_a_MISSING_secret_fails_CLOSED_rather_than_booting_tokenless(monkeypatch: pytest.MonkeyPatch) -> None:
    """Booting without the token is the failure mode that actually happened: the service came up
    healthy, served `/api/health` 200, accepted runs with a 202, and only died at the first catalog
    call — one activity deep inside a workflow, where the 401 reaches an operator as `FAILED` with an
    empty error map. Failing at the fetch turns a mystery into a message."""
    from ingest import catalog_service

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "true")
    monkeypatch.setattr(
        "service_kit.governed.secrets.fetch_required_secrets",
        lambda store, key, *, require: (_ for _ in ()).throw(RuntimeError(f"secret {require!r} unavailable — failing closed")),
    )

    with pytest.raises(RuntimeError, match="unavailable"):
        catalog_service.catalog_token()


def test_the_LOCAL_path_needs_no_token_and_asks_for_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the catalog disabled there is no catalog to authenticate to, so reaching for a secret
    would fail a local run for a credential it has no use for. `catalog_enabled()` is explicit rather
    than inferred precisely so this branch is a decision and not an accident."""
    from ingest import catalog_service

    monkeypatch.delenv("RASK_INGEST_USE_CATALOG", raising=False)

    def _explode(store: str, key: str, *, require: str) -> dict[str, str]:
        raise AssertionError("the local path asked the secret store for a token it does not need")

    monkeypatch.setattr("service_kit.governed.secrets.fetch_required_secrets", _explode)

    assert catalog_service.catalog_token() is None


def test_the_bearer_actually_reaches_the_REQUEST_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving the token and then not sending it would look identical from the outside — a 401 from
    the catalog either way — so the header assembly is pinned too."""
    import pyarrow as pa

    from ingest import catalog_service

    monkeypatch.setenv("RASK_INGEST_USE_CATALOG", "true")
    monkeypatch.setattr(
        "service_kit.governed.secrets.fetch_required_secrets",
        lambda store, key, *, require: {require: "tok-123"},
    )

    client = catalog_service.CatalogServiceClient(pa.schema([pa.field("id", pa.int64())]))

    assert client._headers()["Authorization"] == "Bearer tok-123"
    # Extra headers survive alongside it — the Arrow create path sends a Content-Type.
    merged = client._headers({"Content-Type": "application/vnd.apache.arrow.stream"})
    assert merged["Content-Type"] == "application/vnd.apache.arrow.stream"
    assert merged["Authorization"] == "Bearer tok-123"
