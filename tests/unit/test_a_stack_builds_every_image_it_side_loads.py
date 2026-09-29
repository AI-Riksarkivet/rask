"""A kind stack must build every side-loaded image the chart it deploys actually schedules.

`image.localImages=true` means a bare `<component>:<tag>` reference resolves on the NODE — so an
image the script does not build and `kind load` cannot be pulled from anywhere. It is not a slow
start: containerd asks Docker Hub for `docker.io/library/notifications:dev`, is told
`pull access denied, repository does not exist`, and the pod sits in ImagePullBackOff forever.

MEASURED 2026-09-24 on `e2e-stack`: the overlay schedules SEVEN rask images and the script builds
ONE. The commit that most recently touched this was titled "both kind stacks build and load every
image" — mine, and wrong; it fixed the chart-values half and left the build list alone, and nothing
compared the two.

DERIVED FROM THE RENDER, never a hand-list. The set of side-loaded images is whatever the chart
schedules under that stack's own overlay, so a new service joins it by existing — which is exactly
what a hand-maintained list in a shell script cannot do. Third-party references are excluded by
carrying a registry path; `busybox`/`nats` are Docker Hub library images and carry the `:dev`-free
tags this filter keys on.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from tests.unit.chart_render import OIDC_ARGS, render
from tests.unit.test_no_chart_owned_manifest_renders_a_null import _overlays


REPO = pathlib.Path(__file__).resolve().parents[2]

#: THE SCRIPTS' OWN EXTRACTION, not a second implementation of it. This gate held its own walker over
#: the parsed render while the stack scripts grepped their raw text for the same thing, and the two
#: disagreed in the one dimension a text filter is blind to: every chart call site passes `rask.image`
#: through `quote`, so the shell matched none of the fifteen `:dev` references the gate read happily —
#: and the gate returned green on the SHAPE of a pipeline that found nothing. One function, imported.
_SPEC = importlib.util.spec_from_file_location("side_loaded_images", REPO / "scripts" / "side_loaded_images.py")
assert _SPEC and _SPEC.loader
_SLI = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SLI)


def _side_loaded(overlay: tuple[str, ...]) -> set[str]:
    return set(_SLI.side_loaded_in(render(*overlay, *OIDC_ARGS), "dev"))


def _stem(image: str) -> str:
    return str(_SLI.dockerfile_stem(image))


@pytest.mark.parametrize("label,overlay", [(lbl, ov) for lbl, ov in _overlays() if lbl.endswith(".sh")], ids=lambda v: v if isinstance(v, str) else "")
def test_every_side_loaded_image_has_a_dockerfile_to_build_it(label: str, overlay: tuple[str, ...]) -> None:
    """The other half: a name the stack could build only if something knows how."""
    orphans = sorted(image for image in _side_loaded(overlay) if not (REPO / ".docker" / f"{_stem(image)}.dockerfile").is_file())
    assert not orphans, f"the {label} overlay schedules images with no `.docker/<stem>.dockerfile` to build them:\n  " + "\n  ".join(orphans)


def test_a_reference_the_chart_quotes_is_still_found() -> None:
    """The defect in miniature: every `image:` in a real render is a QUOTED scalar.

    `chart/templates/*.yaml` pipe `rask.image` through `quote`, so `image: "gateway:dev"` is the only
    form that reaches a stack script. A filter anchored on the unquoted scalar found zero of the
    fifteen references the `e2e-stack` overlay schedules, and a stack that finds no image aborts —
    measured 2026-09-24, both live lanes died there before deploying anything.
    """
    render = 'spec:\n  containers:\n    - image: "gateway:dev"\n    - image: bare-stem:dev\n'
    assert _SLI.side_loaded(render, "dev") == ["bare-stem:dev", "gateway:dev"]


def test_an_image_that_is_not_ours_is_left_alone() -> None:
    """Bare is not the test — the TAG is. `busybox` and `nats` carry no registry path either, and
    handing either to `dagger-image.sh --name` would fail on a dockerfile that does not exist."""
    render = (
        "spec:\n"
        "  containers:\n"
        '    - image: "busybox:1.36"\n'
        '    - image: "nats:2.14.2-alpine"\n'
        '    - image: "registry.k8s.io/kubectl:v1.31.3"\n'
        '    - image: "ghcr.io/org/gateway:dev"\n'
        '    - image: "gateway@sha256:0000000000000000000000000000000000000000000000000000000000000000"\n'
    )
    assert _SLI.side_loaded(render, "dev") == []
