"""Secret consumption from the Dapr store — the audit's 'wired but never read' / 'plaintext still
ships' fix. Verifies the store is the SOLE source (no plaintext fallback when on), the fail-closed
behaviour, and that the AGE DB password is spliced into the connection string from the store."""

from __future__ import annotations

import httpx
import pytest

from lineage.core.config import LineageSettings, _with_db_password, apply_lineage_secrets
from service_kit.governed.secrets import fetch_dapr_secret


def test_with_db_password_splices_userinfo() -> None:
    url = "postgresql://lance@age:5432/lineage"
    assert _with_db_password(url, "s3cr3t") == "postgresql://lance:s3cr3t@age:5432/lineage"


def test_with_db_password_url_encodes_special_chars() -> None:
    url = "postgresql://lance@age:5432/lineage"
    # A password with URL-significant chars must be percent-encoded so the DSN stays parseable.
    assert _with_db_password(url, "p@ss/w:rd") == "postgresql://lance:p%40ss%2Fw%3Ard@age:5432/lineage"


def test_fetch_dapr_secret_fails_fast_on_4xx_but_retries_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resilience-skill rule pinned: a 4xx is MISCONFIGURATION (bad store name, denied scope) that
    waiting can't heal — exactly ONE attempt, not a burned boot-retry budget; a transient connect
    failure retries and succeeds. Backoff is jittered-exponential via tenacity (initial=backoff)."""
    calls = {"n": 0}

    def _forbidden(url: str, timeout: float) -> httpx.Response:
        calls["n"] += 1
        request = httpx.Request("GET", url)
        return httpx.Response(403, request=request)

    monkeypatch.setattr(httpx, "get", _forbidden)
    assert fetch_dapr_secret("store", "key", retries=5, backoff=0.001) == {}
    assert calls["n"] == 1  # non-transient → no retries

    flaky = {"n": 0}

    def _flaky(url: str, timeout: float) -> httpx.Response:
        flaky["n"] += 1
        if flaky["n"] < 3:
            raise httpx.ConnectError("sidecar not up yet", request=httpx.Request("GET", url))
        return httpx.Response(200, json={"k": "v"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", _flaky)
    assert fetch_dapr_secret("store", "key", retries=5, backoff=0.001) == {"k": "v"}
    assert flaky["n"] == 3  # two transient failures retried, then the success returned


def test_apply_lineage_secrets_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fail(*_args: object, **_kwargs: object) -> dict[str, str]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fail)
    # model_validate (not LineageSettings(...)) so field-name keys validate cleanly past ty: the fields
    # carry LINEAGE_* aliases and populate_by_name accepts the names only at runtime.
    settings = LineageSettings.model_validate({"secrets_from_dapr": False})
    apply_lineage_secrets(settings)
    assert not called  # off → never touches the store


def test_apply_lineage_secrets_consumes_store_as_sole_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "service_kit.governed.secrets.fetch_dapr_secret",
        lambda *_a, **_k: {"rustfs-secret-key": "from-store", "postgres-password": "db-from-store"},
    )
    settings = LineageSettings.model_validate({"secrets_from_dapr": True, "database_url": "postgresql://lance@age:5432/lineage"})
    apply_lineage_secrets(settings)
    assert settings.s3_secret_access_key.get_secret_value() == "from-store"  # SecretStr now
    assert settings.database_url == "postgresql://lance:db-from-store@age:5432/lineage"


def test_apply_lineage_secrets_fails_closed_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # Store empty AND no env fallback → must raise, never boot with an empty S3 key.
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: {})
    settings = LineageSettings.model_validate({"secrets_from_dapr": True, "s3_secret_access_key": ""})
    with pytest.raises(RuntimeError, match="failing closed"):
        apply_lineage_secrets(settings)


def test_apply_lineage_secrets_keeps_db_url_when_store_lacks_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # S3 secret present (so it doesn't fail closed) but no DB password in the bundle → URL unchanged.
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: {"rustfs-secret-key": "x"})
    settings = LineageSettings.model_validate({"secrets_from_dapr": True, "database_url": "postgresql://lance:envpw@age:5432/lineage"})
    apply_lineage_secrets(settings)
    assert settings.database_url == "postgresql://lance:envpw@age:5432/lineage"
