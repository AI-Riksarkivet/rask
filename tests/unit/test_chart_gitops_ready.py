"""The chart is consumed by a GitOps reconciler, and these are the two properties that requires.

Neither was true before 2026-08-04, and both failed the same way: silently at render time, loudly
much later in a cluster nobody was watching.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _helm() -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    return helm


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    argv = [_helm(), "template", "rask", str(CHART)]
    for value in sets:
        argv += ["--set", value]
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


def test_a_bare_image_name_cannot_be_rendered_by_accident() -> None:
    """`image.repository: ""` used to render `gateway:dev` — i.e. `docker.io/library/gateway:dev`.

    It only ever appeared to work because `make k3s-import` had side-loaded the tag into the node's
    containerd, so the kubelet never pulled. A reconciler has no side channel: every pod
    ImagePullBackOffs and the error blames Docker Hub rather than the missing setting. Observed
    2026-08-04 on controlplane:dev and compute:dev after a `helm upgrade` reset the deployments to
    the chart defaults.
    """
    proc = _render()
    assert proc.returncode != 0, "a registry-less render must FAIL, not emit a Docker Hub reference"
    assert "image.repository must be set" in proc.stderr


def test_side_loaded_images_remain_supported_but_only_as_an_explicit_opt_in() -> None:
    """`make k3s-up`, the Tiltfile and the chart tests all take this path deliberately."""
    proc = _render("image.localImages=true")
    assert proc.returncode == 0, proc.stderr
    assert 'image: "gateway:dev"' in proc.stdout


def test_a_digest_pins_every_first_party_image_and_beats_the_tag() -> None:
    """A tag is a mutable pointer; a digest is what an image-automation controller writes back into
    git, and the only reference that cannot drift under the commit that claims it."""
    proc = _render(
        "image.repository=ghcr.io/example/rask",
        "image.digest=sha256:abc123",
        "frontend.enabled=true",
    )
    assert proc.returncode == 0, proc.stderr
    assert "ghcr.io/example/rask/gateway@sha256:abc123" in proc.stdout
    assert "ghcr.io/example/rask/web-home@sha256:abc123" in proc.stdout
    # …and the tag it would otherwise have used is gone from every first-party ref.
    assert "ghcr.io/example/rask/gateway:dev" not in proc.stdout
