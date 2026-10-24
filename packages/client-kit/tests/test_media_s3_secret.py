"""The media plane's S3 SECRET comes from the Dapr secret store, fail-closed — never the env chain.

The estate's rule (2026-08-07 ruling: NO secret in env anywhere): coordinates are config, the secret is not. Before
this, `storage_options`
silently fell back to the AWS env chain — an env-borne secret with a configured look.

`storage_options` IS A METHOD, NOT A PROPERTY (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit", 2026-08-28). It
performs a blocking
Dapr secret fetch and can raise — work that a `@property` disguised as a free attribute read, which is
exactly how it reached an event loop (`datasets.py::list_datasets` built its registry inline). A
method makes the cost visible at every call site, and it aligns this config with every OTHER config in
the estate (`MedallionSettings`, the catalog and maintenance settings all expose `storage_options()`).
"""

from __future__ import annotations

import pytest

from client_kit.media import config as media_config
from client_kit.media.config import Settings


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
    opts = _settings().storage_options()
    assert opts is not None
    assert opts["aws_secret_access_key"] == "from-the-store"
    assert opts["aws_access_key_id"] == "rustfsadmin"


def test_missing_store_secret_FAILS_CLOSED(monkeypatch: pytest.MonkeyPatch) -> None:
    """No bundle -> RAISE. A fallback to the AWS env chain would re-admit env secrets while
    looking configured — the exact failure mode the rule names."""
    monkeypatch.setattr(media_config, "_store_secret", lambda *_a: None)
    with pytest.raises(RuntimeError, match="failing closed"):
        _settings().storage_options()


def test_missing_access_key_id_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_config, "_store_secret", lambda *_a: "irrelevant")
    with pytest.raises(RuntimeError, match="MEDIA_S3_ACCESS_KEY_ID"):
        Settings.model_validate({"MEDIA_S3_ENDPOINT": "http://rustfs:9000"}).storage_options()


def test_explicit_test_creds_bypass_the_store() -> None:
    """Sidecar-less dev/tests: BOTH static fields set -> no store call (it would raise here)."""
    opts = _settings(MEDIA_S3_SECRET_ACCESS_KEY="local-secret").storage_options()
    assert opts is not None and opts["aws_secret_access_key"] == "local-secret"


def test_local_root_needs_no_store_at_all() -> None:
    assert Settings.model_validate({}).storage_options() is None


@pytest.mark.parametrize(("endpoint", "expected"), [("HTTP://rustfs:9000", "true"), ("HTTPS://s3.example.com", "false")])
def test_allow_http_follows_the_scheme_in_any_case(endpoint: str, expected: str) -> None:
    """The media builder derives `allow_http` by the lakehouse's one case-insensitive rule
    (`service_kit.lakehouse.endpoint_scheme`): object_store lowercases the scheme, so a case-sensitive check would
    hand an upper-case plaintext endpoint `false` and the client would never be built."""
    # Both static fields set, so the builder takes its no-secret-store branch.
    options = _settings(MEDIA_S3_ENDPOINT=endpoint, MEDIA_S3_SECRET_ACCESS_KEY="SK").storage_options()
    assert options is not None
    assert options["allow_http"] == expected
