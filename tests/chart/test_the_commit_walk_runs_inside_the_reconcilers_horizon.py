"""The catalog walks every claimed table for a committed version the feed lacks more often than the reconciler's horizon.

A version committed past every door files no entry for the reconcile tick, so only the walk counts it ([[XC-204]]), and
neither looks for a version older than `catalog.commitReconcile.horizonSeconds`. A walk period at or past the horizon
would let such a version age out unseen, so the render refuses it.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.chart.chart_render import REPO, containers, env_of, render, render_chart_text


_BASE = ("--set", "image.localImages=true")


def test_the_catalog_is_told_the_walks_period_the_chart_declares() -> None:
    (catalog,) = [container for name, process, container in containers(render(*_BASE)) if f"{name}/{process}" == "Deployment/rask-catalog/catalog"]

    assert env_of(catalog)["LANCE_COMMIT_WALK_PERIOD_SECONDS"] == "3600"


def test_a_walk_no_more_frequent_than_the_horizon_fails_the_render() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *_BASE, "--set", "catalog.commitReconcile.walkPeriodSeconds=86400")

    assert "walkPeriodSeconds must be shorter than catalog.commitReconcile.horizonSeconds" in refused.value.stderr, refused.value.stderr[-600:]
