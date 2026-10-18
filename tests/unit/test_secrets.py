"""Secret consumption from the Dapr store — the audit's 'wired but never read' / 'plaintext still
ships' fix. Verifies the store is the SOLE source (no plaintext fallback when on) and the fail-closed
behaviour, on the settings lineage boots with."""

from __future__ import annotations

import httpx
import pytest

from lineage.core.config import LineageSettings, storage_options
from service_kit.governed.secrets import fetch_dapr_secret
from service_kit.governed.store_credential import StoreCredential, make_store_credential


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
    assert make_store_credential(settings) is None
    storage_options(settings)
    assert not called  # off → never touches the store


def test_lineage_takes_the_outbox_pair_from_the_store_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "service_kit.governed.secrets.fetch_dapr_secret",
        lambda *_a, **_k: {"lineage-s3-access-key": "id-from-store", "lineage-s3-secret-key": "from-store"},
    )
    settings = LineageSettings.model_validate({"secrets_from_dapr": True, "s3_access_key_id": "id-from-env", "s3_secret_access_key": "from-env"})
    StoreCredential(settings).read_at_boot()
    options = storage_options(settings)
    assert (options["aws_access_key_id"], options["aws_secret_access_key"]) == ("id-from-store", "from-store")


@pytest.mark.parametrize("bundle", [{}, {"lineage-s3-secret-key": "only-the-secret"}], ids=["store-empty", "secret-without-its-key-id"])
def test_lineage_refuses_to_boot_without_the_whole_outbox_pair(monkeypatch: pytest.MonkeyPatch, bundle: dict[str, str]) -> None:
    # Store empty, or half a pair, AND no env fallback → must raise, never boot signing with half a credential.
    monkeypatch.setattr("service_kit.governed.secrets.fetch_dapr_secret", lambda *_a, **_k: dict(bundle))
    settings = LineageSettings.model_validate({"secrets_from_dapr": True, "s3_secret_access_key": ""})
    with pytest.raises(RuntimeError, match="failing closed|half a credential"):
        StoreCredential(settings).read_at_boot()
