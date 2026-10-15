"""Secret consumption from the Dapr store — the audit's 'wired but never read' / 'plaintext still
ships' fix. Verifies the store is the SOLE source (no plaintext fallback when on) and the fail-closed
behaviour, on the settings lineage boots with."""

from __future__ import annotations

import httpx
import pytest

from lineage.core.config import LineageSettings
from service_kit.governed.secrets import apply_dapr_secrets, fetch_dapr_secret


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


def test_lineage_reads_no_store_when_secrets_from_dapr_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fail(*_args: object, **_kwargs: object) -> dict[str, str]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", _fail)
    # model_validate (not LineageSettings(...)) so field-name keys validate cleanly past ty: the fields
    # carry LINEAGE_* aliases and populate_by_name accepts the names only at runtime.
    settings = LineageSettings.model_validate({"secrets_from_dapr": False})
    apply_dapr_secrets(settings)
    assert not called  # off → never touches the store


def test_lineage_takes_the_outbox_secret_from_the_store_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "service_kit.governed.secrets.fetch_dapr_secret",
        lambda *_a, **_k: {"minio-secret-key": "from-store"},
    )
    settings = LineageSettings.model_validate({"secrets_from_dapr": True})
    apply_dapr_secrets(settings)
    assert settings.s3_secret_access_key.get_secret_value() == "from-store"  # SecretStr now


def test_lineage_refuses_to_boot_without_the_outbox_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # Store empty AND no env fallback → must raise, never boot with an empty S3 key.
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: {})
    settings = LineageSettings.model_validate({"secrets_from_dapr": True, "s3_secret_access_key": ""})
    with pytest.raises(RuntimeError, match="failing closed"):
        apply_dapr_secrets(settings)
