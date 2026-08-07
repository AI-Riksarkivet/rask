"""The media plane's S3 SECRET comes from the Dapr secret store, fail-closed — never the env chain.

The estate's rule (2026-08-07 ruling: NO secret in env anywhere) and the in-file precedent
(`MEDIA_PUBLISH_*`: coordinates are config, the password is not). Before this, `storage_options`
silently fell back to the AWS env chain — an env-borne secret with a configured look.
"""

from __future__ import annotations

import pytest

from service_kit.media import config as media_config
from service_kit.media.config import Settings


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {"MEDIA_S3_ENDPOINT": "http://rustfs:9000", "MEDIA_S3_ACCESS_KEY_ID": "rustfsadmin"}
    base.update(over)
    return Settings.model_validate(base)


@pytest.fixture(autouse=True)
def _fresh_cache():
    # `getattr` guard: at teardown monkeypatch may already have swapped the real (cached) function
    # back in — or not — so the attribute is only conditionally present.
    getattr(media_config._store_secret, "cache_clear", lambda: None)()
    yield
    getattr(media_config._store_secret, "cache_clear", lambda: None)()


def test_secret_comes_from_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_config, "_store_secret", lambda *_a: "from-the-store")
    opts = _settings().storage_options
    assert opts is not None
    assert opts["secret_access_key"] == "from-the-store"
    assert opts["access_key_id"] == "rustfsadmin"


def test_missing_store_secret_FAILS_CLOSED(monkeypatch: pytest.MonkeyPatch) -> None:
    """No bundle -> RAISE. A fallback to the AWS env chain would re-admit env secrets while
    looking configured — the exact failure mode the rule names."""
    monkeypatch.setattr(media_config, "_store_secret", lambda *_a: None)
    with pytest.raises(RuntimeError, match="failing closed"):
        _ = _settings().storage_options


def test_missing_access_key_id_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_config, "_store_secret", lambda *_a: "irrelevant")
    with pytest.raises(RuntimeError, match="MEDIA_S3_ACCESS_KEY_ID"):
        _ = Settings.model_validate({"MEDIA_S3_ENDPOINT": "http://rustfs:9000"}).storage_options


def test_explicit_test_creds_bypass_the_store() -> None:
    """Sidecar-less dev/tests: BOTH static fields set -> no store call (it would raise here)."""
    opts = _settings(MEDIA_S3_SECRET_ACCESS_KEY="local-secret").storage_options
    assert opts is not None and opts["secret_access_key"] == "local-secret"


def test_local_root_needs_no_store_at_all() -> None:
    assert Settings.model_validate({}).storage_options is None
