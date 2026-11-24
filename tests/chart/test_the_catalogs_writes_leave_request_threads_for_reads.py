"""The catalog's writers never hold every request thread.

Each write the shed admits (`catalog.maxConcurrentWrites`) holds one thread of the request pool
(`services.catalog.requestThreads`) while it commits, four more threads may wait for a commit turn, and every read
needs one too ([[XC-204]]). So the chart tells the catalog its pool and its cap, and refuses a cap that, with those four,
reaches the pool; the values schema refuses no cap at all.
"""

from __future__ import annotations

import subprocess

import pytest

from lakehouse.records.commit_turns import THREAD_WAITERS
from tests.chart.chart_render import REPO, containers, env_of, render, render_chart_text


_BASE = ("--set", "image.localImages=true")


def test_the_catalog_is_told_its_thread_pool_and_a_write_cap_below_it() -> None:
    (catalog,) = [container for name, process, container in containers(render(*_BASE)) if f"{name}/{process}" == "Deployment/rask-catalog/catalog"]

    told = env_of(catalog)

    assert (told["LANCE_REQUEST_THREADS"], told["LANCE_MAX_CONCURRENT_WRITES"]) == ("24", "16")


def test_a_write_cap_that_with_the_turn_waiters_reaches_the_pool_fails_the_render() -> None:
    """The render counts the same waiters the catalog seats (`commit_turns.THREAD_WAITERS`): a cap that reaches the pool
    with them is refused here, and :func:`test_the_largest_write_cap_the_catalog_takes_renders` holds the other side."""
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *_BASE, "--set", "services.catalog.requestThreads=24", "--set", f"catalog.maxConcurrentWrites={24 - THREAD_WAITERS}")

    assert "must stay below services.catalog.requestThreads" in refused.value.stderr, refused.value.stderr[-600:]


def test_the_largest_write_cap_the_catalog_takes_renders() -> None:
    largest = str(24 - THREAD_WAITERS - 1)

    rendered = render(*_BASE, "--set", "services.catalog.requestThreads=24", "--set", f"catalog.maxConcurrentWrites={largest}")

    (catalog,) = [container for name, process, container in containers(rendered) if f"{name}/{process}" == "Deployment/rask-catalog/catalog"]
    assert env_of(catalog)["LANCE_MAX_CONCURRENT_WRITES"] == largest
