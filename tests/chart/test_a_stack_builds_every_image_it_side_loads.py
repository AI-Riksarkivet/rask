"""A kind stack must build every side-loaded image the chart it deploys actually schedules.

`image.localImages=true` means a bare `<component>:<tag>` reference resolves on the NODE — so an
image the script does not build and `kind load` cannot be pulled from anywhere. It is not a slow
start: containerd asks Docker Hub for `docker.io/library/<component>:dev`, is told
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

from types import ModuleType

import pytest

from tests.script_module import script


#: THE SCRIPTS' OWN EXTRACTION, not a second implementation of it. This gate held its own walker over
#: the parsed render while the stack scripts grepped their raw text for the same thing, and the two
#: disagreed in the one dimension a text filter is blind to: every chart call site passes `rask.image`
#: through `quote`, so the shell matched none of the fifteen `:dev` references the gate read happily —
#: and the gate returned green on the SHAPE of a pipeline that found nothing. One function, imported.
@pytest.fixture
def side_loaded_images(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """`scripts/side_loaded_images.py`."""
    return script("side_loaded_images", monkeypatch)


def test_a_reference_the_chart_quotes_is_still_found(side_loaded_images: ModuleType) -> None:
    """The defect in miniature: every `image:` in a real render is a QUOTED scalar.

    `chart/templates/*.yaml` pipe `rask.image` through `quote`, so `image: "gateway:dev"` is the only
    form that reaches a stack script. A filter anchored on the unquoted scalar found zero of the
    fifteen references the `e2e-stack` overlay schedules, and a stack that finds no image aborts —
    measured 2026-09-24, both live lanes died there before deploying anything.
    """
    render = 'spec:\n  containers:\n    - image: "gateway:dev"\n    - image: bare-stem:dev\n'
    assert side_loaded_images.side_loaded(render, "dev") == ["bare-stem:dev", "gateway:dev"]


def test_a_workload_scaled_to_zero_asks_the_node_for_no_image(side_loaded_images: ModuleType) -> None:
    """`replicas: 0` schedules no pod, so its image is never pulled and the stack need not build it.

    A lane that turns a service off this way would otherwise spend a build on an image nothing runs. The
    same image named by a workload that does run is still asked for.
    """
    render = (
        "kind: Deployment\nspec:\n  replicas: 0\n  template:\n    spec:\n      containers:\n"
        '        - image: "scaled-to-zero:dev"\n        - image: "lance-rest-catalog:dev"\n'
        "---\nkind: Deployment\nspec:\n  replicas: 1\n  template:\n    spec:\n      containers:\n"
        '        - image: "lance-rest-catalog:dev"\n'
        "---\nkind: Job\nspec:\n  template:\n    spec:\n      containers:\n"
        '        - image: "one-shot:dev"\n'
    )
    assert side_loaded_images.side_loaded(render, "dev") == ["lance-rest-catalog:dev", "one-shot:dev"]


def test_an_image_that_is_not_ours_is_left_alone(side_loaded_images: ModuleType) -> None:
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
    assert side_loaded_images.side_loaded(render, "dev") == []
