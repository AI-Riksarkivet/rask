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

import pathlib
import re

import pytest

from tests.unit.chart_render import OIDC_ARGS, render
from tests.unit.test_no_chart_owned_manifest_renders_a_null import _overlays


REPO = pathlib.Path(__file__).resolve().parents[2]

#: A side-loaded reference: no registry path, and the local tag `image.localImages` renders.
_SIDE_LOADED = re.compile(r"^([a-z0-9][a-z0-9-]*):dev$")

#: `lance-rest-catalog:dev` is built from `.docker/rest-catalog.dockerfile`. The prefix is the only
#: difference between a rendered image name and its dockerfile stem anywhere in the estate.
_STEM_PREFIX = "lance-"


def _side_loaded(overlay: tuple[str, ...]) -> set[str]:
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "image" and isinstance(value, str) and _SIDE_LOADED.match(value):
                    found.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for doc in render(*overlay, *OIDC_ARGS):
        walk(doc.get("spec") or {})
    return found


def _stem(image: str) -> str:
    return image.split(":")[0].removeprefix(_STEM_PREFIX)


#: A script that DERIVES its side-loaded set from the chart render rather than listing it.
_DERIVES = re.compile(r"SIDE_LOADED=.*helm\.sh\" template", re.DOTALL)

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
