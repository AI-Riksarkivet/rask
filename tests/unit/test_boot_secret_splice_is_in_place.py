"""The boot-time secret splice mutates the CACHED settings object, and the request path depends on it.

Four services consume the S3 secret from the Dapr secret store in their lifespan and then serve every
later read out of the same ``@lru_cache``d ``get_settings()``, over five call sites: ``catalog.main``,
``medallion.producer``, ``medallion.mover``, ``maintenance.service`` and ``lineage.main`` (through
``apply_lineage_secrets``, which splices the AGE password onto the same object). The splice
(``service_kit.governed.secrets.apply_dapr_secrets``) therefore assigns onto the cached instance IN
PLACE — that is the mechanism, not an accident, and it is what makes the per-request ``SettingsDep``
read correct.

It reads like a bug, so it attracts "fixes". The two obvious ones both break production SILENTLY: the
boot succeeds, every pod goes ready, and the first object-store call signs with an empty key.

* Freeze the model (``model_config = SettingsConfigDict(frozen=True)``) — the splice raises at boot, or,
  if the assignment is "fixed" alongside it, degrades into one of the copies below.
* Copy on write (``settings.model_copy(update=...)``, or returning a new ``Settings``) — the lifespan's
  local name carries the secret, the cache still holds the object without it, and ``SettingsDep`` hands
  every request the stale one.

These tests fail on both. They are the pin, not a bug report: today's behaviour is correct.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from service_kit.governed.secrets import SupportsDaprSecrets, apply_dapr_secrets


FROM_STORE = "spliced-at-boot"


def _accessors() -> list[tuple[str, Callable[[], Any], dict[str, str]]]:
    """(label, the service's cached accessor, the env that makes it consume the store) for every service
    whose lifespan splices its settings at boot."""
    from catalog.core.config import get_settings as catalog_settings
    from lineage.core.config import get_settings as lineage_settings
    from maintenance.core.config import get_settings as maintenance_settings
    from medallion.core.config import get_settings as medallion_settings

    return [
        ("catalog", catalog_settings, {"LANCE_S3_ACCESS_KEY_ID": "x", "LANCE_SECRETS_FROM_DAPR": "true"}),
        ("medallion", medallion_settings, {"MEDALLION_SECRETS_FROM_DAPR": "true"}),
        ("maintenance", maintenance_settings, {"MAINTENANCE_SECRETS_FROM_DAPR": "true"}),
        ("lineage", lineage_settings, {"LINEAGE_SECRETS_FROM_DAPR": "true"}),
    ]


@pytest.fixture(autouse=True)
def _isolate_the_process_wide_caches() -> Iterator[None]:
    """These tests drive the REAL cached accessors, which is the point — so clear the caches around them
    rather than leaving a spliced singleton behind for the rest of the session."""
    accessors = [accessor for _, accessor, _ in _accessors()]

    def _clear() -> None:
        for accessor in accessors:
            # `getattr`, so an accessor that LOST its cache still reaches the assertion that names why
            # that is fatal, instead of erroring out here on a missing `cache_clear`.
            getattr(accessor, "cache_clear", lambda: None)()

    _clear()
    yield
    _clear()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the store fetch so no test needs a sidecar."""
    import service_kit.governed.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "fetch_required_secrets", lambda _s, _k, *, require: {require: FROM_STORE})


@pytest.mark.usefixtures("store")
@pytest.mark.parametrize("label", [row[0] for row in _accessors()])
def test_the_boot_splice_reaches_every_later_read_of_the_cached_settings(label: str, monkeypatch: pytest.MonkeyPatch) -> None:
    accessor, env = next((a, e) for lbl, a, e in _accessors() if lbl == label)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    boot: SupportsDaprSecrets = accessor()  # the object the lifespan holds
    apply_dapr_secrets(boot)
    later = accessor()  # what SettingsDep — and every non-request reader — resolves afterwards

    assert later is boot, "get_settings() stopped being a cached singleton: the boot splice reaches nothing"
    assert later.s3_secret_access_key.get_secret_value() == FROM_STORE, "the splice did not land on the cached object (frozen model, or copy-on-write)"


@pytest.mark.usefixtures("store")
def test_the_catalog_signs_object_store_requests_with_the_spliced_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The consequence, through the real per-request dependency: `StorageOptionsDep` is what the data
    plane signs S3 with, and it is recomputed from the cached settings on every request."""
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")  # `catalog.main` builds settings at import
    monkeypatch.setenv("LANCE_SECRETS_FROM_DAPR", "true")

    from catalog.api.dependencies import get_storage_options
    from catalog.core.config import get_settings
    from catalog.main import consume_dapr_secrets

    consume_dapr_secrets(get_settings())  # exactly what the lifespan does, before it yields

    assert get_storage_options(get_settings())["secret_access_key"] == FROM_STORE
