"""A released chart runs the images it was released with, named by digest ([[XC-187]], [[XC-202]]).

A client's git names the lakehouse by one chart version, so the packaged chart carries its own images: the release
step writes the registry and each image index's digest (the catalog's and the console's) into the chart's defaults, and
the version into Chart.yaml. An install that sets nothing about images then pulls exactly what was published and
signed, on any architecture each index holds.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from chart_render import PROD_ARGS, REPO, containers, render_chart_text


DIGESTS = {"lance-rest-catalog": "sha256:" + "ab" * 32, "web-console": "sha256:" + "cd" * 32}
REGISTRY = "ghcr.io/ai-riksarkivet"


def test_a_released_chart_runs_the_images_it_was_released_with(tmp_path: Path) -> None:
    chart = tmp_path / "chart"
    shutil.copytree(REPO / "chart", chart)

    subprocess.run(
        [sys.executable, str(REPO / "scripts/pin_chart_release.py"), str(chart), "--version", "0.4.0", "--repository", REGISTRY,
         *(arg for image, digest in DIGESTS.items() for arg in ("--digest", f"{image}={digest}"))],
        check=True,
    )  # fmt: skip

    # A real deployment's inputs, less the overlay's own image source: the released chart's is the claim.
    pairs = list(zip(PROD_ARGS[::2], PROD_ARGS[1::2], strict=True))
    rendered = render_chart_text(chart, *(arg for pair in pairs if not pair[1].startswith("image.") for arg in pair))
    docs = tuple(doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict))
    first_party = {c["image"] for _, _, c in containers(docs) if any(image in c["image"] for image in DIGESTS)}
    assert first_party == {f"{REGISTRY}/{image}@{digest}" for image, digest in DIGESTS.items()}
    released = yaml.safe_load((chart / "Chart.yaml").read_text())
    assert (released["version"], released["appVersion"]) == ("0.4.0", "0.4.0")
