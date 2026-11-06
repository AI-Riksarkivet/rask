"""The console's release as `make k3s-clients` renders it, for the suites in this directory.

The console is a client: its tests live in its own tree, so deleting `deploy/fixtures/console` deletes them and leaves
the lakehouse's suites green. They read rask's chart and fixtures (a client knows the lakehouse); nothing of the
lakehouse reads them.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest


# The repository root, so `tests.unit.chart_render` resolves under importlib mode whichever suite pytest loads first, as
# services/catalog/tests/conftest.py puts it there for `tests.integration.store_requests`.
sys.path.insert(0, str(Path(__file__).parents[4]))

from tests.unit.chart_render import render_fixture


#: The capabilities the console's release refuses to render without, as a cluster with its prerequisites serves them.
PREREQUISITES: Final = (
    "--api-versions", "dapr.io/v1alpha1",
    "--api-versions", "postgresql.cnpg.io/v1/Cluster",
    "--api-versions", "generators.external-secrets.io/v1alpha1/Password",
    "--api-versions", "external-secrets.io/v1/ExternalSecret",
)  # fmt: skip


@pytest.fixture
def render_console() -> Callable[..., tuple[dict, ...]]:
    """Render the console's release with its prerequisites served, under the values a test adds."""

    def render(*extra: str) -> tuple[dict, ...]:
        return render_fixture("console", *PREREQUISITES, *extra)

    return render
