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
import re

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


#: A script that DERIVES its side-loaded set through that module rather than listing or grepping it.
_DERIVES = re.compile(r"SIDE_LOADED=.*side_loaded_images\.py", re.DOTALL)

#: A private text filter over `image:` — the shape that matched nothing for a day.
_OWN_FILTER = re.compile(r"grep[^\n]*image:")

#: What the script actually BUILDS: `scripts/dagger-image.sh --name <stem>` / `--runner <stem>`.
_BUILDS = re.compile(r"dagger-image\.sh[^\n|;]*?--(?:name|runner)[= ]([a-z0-9][a-z0-9-]*)")


def _script_for(label: str) -> pathlib.Path:
    return REPO / "scripts" / label


def _built_stems(text: str) -> set[str]:
    """Stems the script hands to the image builder.

    NOT a substring search of the whole script, which is how the first cut of this gate passed for
    two images it should have failed: `gateway` appears in a rollout-wait loop and `compute` in a
    comment, and neither is a build. A gate that matches prose reports whatever the prose happens to
    mention.
    """
    return set(_BUILDS.findall(text))


@pytest.mark.parametrize("label,overlay", [(lbl, ov) for lbl, ov in _overlays() if lbl.endswith(".sh")], ids=lambda v: v if isinstance(v, str) else "")
def test_the_stack_builds_every_image_its_overlay_schedules(label: str, overlay: tuple[str, ...]) -> None:
    script = _script_for(label)
    assert script.is_file(), f"{label} no longer exists"
    text = script.read_text(encoding="utf-8")
    scheduled = _side_loaded(overlay)
    assert scheduled, f"{label} schedules no side-loaded image — the gate lost its subject"
    built = _built_stems(text)
    assert built, f"{label} invokes the image builder for nothing — the gate lost its subject"
    if _DERIVES.search(text):
        # The script asks the RENDER which images it side-loads and builds each, so there is no
        # static list to fall behind. That is the shape this gate wants; it stays to catch a revert
        # to a hand-kept one, which is exactly what left six images unbuilt.
        return
    missing = sorted(image for image in scheduled if _stem(image) not in built)
    assert not missing, (
        f"{label} deploys with image.localImages=true but never builds these images its overlay "
        f"schedules, so each pod sits in ImagePullBackOff against a registry that has no such "
        f"repository:\n  " + "\n  ".join(missing)
    )


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


@pytest.mark.parametrize("label,overlay", [(lbl, ov) for lbl, ov in _overlays() if lbl.endswith(".sh")], ids=lambda v: v if isinstance(v, str) else "")
def test_the_stack_extracts_through_the_shared_filter_and_holds_no_copy(label: str, overlay: tuple[str, ...]) -> None:
    """The early return above accepts a derivation on SHAPE, so the shape has to be the shared one.

    A script that greps `image:` out of the render itself is a second implementation of this module,
    and a second implementation is what silently diverged: this gate read the parsed documents while
    the shell read the text, and nothing compared the two answers.
    """
    text = (REPO / "scripts" / label).read_text(encoding="utf-8")
    assert _DERIVES.search(text), f"{label} does not derive its side-loaded set through scripts/side_loaded_images.py"
    own = _OWN_FILTER.search(text)
    assert not own, f"{label} filters `image:` out of the render itself ({own.group(0)!r}) — that copy is the bug this gate exists for"
