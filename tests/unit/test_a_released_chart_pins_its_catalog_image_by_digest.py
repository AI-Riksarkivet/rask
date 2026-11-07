"""A released chart runs the catalog image it was released with, named by digest ([[XC-187]]).

A client's git names the lakehouse by one chart version, so the packaged chart carries its own image: the release
step writes the registry and the image index's digest into the chart's defaults, and the version into Chart.yaml.
An install that sets nothing about images then pulls exactly what was published, on any architecture the index holds.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from chart_render import PROD_ARGS, REPO, containers, render_chart_text


DIGEST = "sha256:" + "ab" * 32
REGISTRY = "ghcr.io/ai-riksarkivet"


def test_a_released_chart_runs_the_catalog_image_it_was_released_with(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    shutil.copytree(REPO / "chart", chart)

    subprocess.run(
        [sys.executable, str(REPO / "scripts/pin_chart_release.py"), str(chart), "--version", "0.4.0", "--repository", REGISTRY,
         "--digest", f"lance-rest-catalog={DIGEST}"],
        check=True,
    )  # fmt: skip

    # A real deployment's inputs, less the overlay's own image source: the released chart's is the claim. A release ships
    # the catalog image alone, so its render runs without the console, whose image no release names (XC-164).
    pairs = list(zip(PROD_ARGS[::2], PROD_ARGS[1::2], strict=True))
    rendered = render_chart_text(chart, *(arg for pair in pairs if not pair[1].startswith("image.") for arg in pair), "--set", "console.enabled=false")
    docs = tuple(doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict))
    catalog_images = {c["image"] for _, _, c in containers(docs) if "lance-rest-catalog" in c["image"]}
    assert catalog_images == {f"{REGISTRY}/lance-rest-catalog@{DIGEST}"}
    released = yaml.safe_load((chart / "Chart.yaml").read_text())
    assert (released["version"], released["appVersion"]) == ("0.4.0", "0.4.0")
