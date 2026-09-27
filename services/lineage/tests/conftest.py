"""Lineage suite fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from lineage.core.config import get_settings


@pytest.fixture(autouse=True)
def _settings_do_not_outlive_the_test() -> Iterator[None]:
    """A route test sets the environment through ``monkeypatch`` and the request re-caches ``get_settings``;
    monkeypatch restores the environment at teardown, and without this the cached settings (FGA and
    OIDC on, say) would answer every later test in the same worker."""
    yield
    get_settings.cache_clear()
