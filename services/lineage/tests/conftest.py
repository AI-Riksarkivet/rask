"""Lineage suite fixtures."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from lineage.core.config import get_settings


# The suite's shared helper (`lineage_lane`) is a plain module beside its tests: under `--import-mode=importlib` nothing
# else puts this directory on the path, and a conftest is loaded before collection begins.
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _settings_do_not_outlive_the_test() -> Iterator[None]:
    """A route test sets the environment through ``monkeypatch`` and the request re-caches ``get_settings``;
    monkeypatch restores the environment at teardown, and without this the cached settings (FGA and
    OIDC on, say) would answer every later test in the same worker."""
    yield
    get_settings.cache_clear()
