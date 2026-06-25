"""Registration smoke test for the htrflow pipeline variant.

Full graph execution requires a running Ray cluster + Serve app, which is
out of scope for unit tests. This test guards against the most common
regression: forgetting to register the new pipeline in the PIPELINES
dict, which would surface to users as `typer.BadParameter: unknown
pipeline 'htrflow'`.
"""

from __future__ import annotations


def test_htrflow_pipeline_is_registered():
    from runner.pipeline import PIPELINES

    assert "htrflow" in PIPELINES
    assert callable(PIPELINES["htrflow"])


def test_htr_and_prefetch_pipelines_still_registered():
    """Defensive: make sure we didn't shadow or drop existing entries."""
    from runner.pipeline import PIPELINES

    for name in ("htr", "prefetch", "fake"):
        assert name in PIPELINES, f"{name!r} disappeared from PIPELINES"
