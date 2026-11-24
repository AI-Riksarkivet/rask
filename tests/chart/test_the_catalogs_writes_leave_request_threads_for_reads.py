"""The catalog's writers never hold every request thread.

Each write the shed admits (`catalog.maxConcurrentWrites`) holds one thread of the request pool
(`services.catalog.requestThreads`) while it commits, four more threads may wait for a commit turn, and every read
needs one too ([[XC-204]]). So the chart tells the catalog its pool and its cap, and refuses a cap that, with those four,
reaches the pool; the values schema refuses no cap at all.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.chart.chart_render import REPO, containers, env_of, render, render_chart_text


_BASE = ("--set", "image.localImages=true")


def test_the_catalog_is_told_its_thread_pool_and_a_write_cap_below_it() -> None:
    (catalog,) = [container for name, process, container in containers(render(*_BASE)) if f"{name}/{process}" == "Deployment/rask-catalog/catalog"]

    told = env_of(catalog)

    assert (told["LANCE_REQUEST_THREADS"], told["LANCE_MAX_CONCURRENT_WRITES"]) == ("24", "16")


def test_a_write_cap_that_with_the_turn_waiters_reaches_the_pool_fails_the_render() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *_BASE, "--set", "catalog.maxConcurrentWrites=20")

    assert "must stay below services.catalog.requestThreads" in refused.value.stderr, refused.value.stderr[-600:]
