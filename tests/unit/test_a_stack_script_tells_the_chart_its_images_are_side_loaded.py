"""A stack script that side-loads images must tell the chart so, or its deploy cannot render.

`rask.image` refuses a render that names neither a registry nor side-loading: *"image.repository must
be set to a registry … or set image.localImages=true if the images are side-loaded into the node"*.
Both kind stacks build with Dagger and `kind load docker-image` at `:dev` — the textbook side-load —
and neither said so, so `helm upgrade` failed AFTER every image had been built and loaded.

MEASURED 2026-09-24 on `e2e-ray`, the first run that got past the kubeconfig guard far enough to reach
a deploy: `execution error at (rask/templates/services.yaml:47:21): image.repository must be set to a
registry`. `e2e-stack` carries the identical defect and had simply never got that far — which is the
shape of every finding in this lane's chain: the next one is only visible once the previous is fixed.

DERIVED FROM THE SCRIPT'S OWN BEHAVIOUR, not from a list of script names: a script that runs
`kind load docker-image` has side-loaded, and must pass the flag.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = sorted((_ROOT / "scripts").glob("*.sh"))


def _side_loaders() -> list[Path]:
    return [p for p in _SCRIPTS if "kind load docker-image" in p.read_text(encoding="utf-8")]


def test_the_scan_finds_the_side_loading_scripts() -> None:
    """Anti-vacuity: the parametrisation below is empty if the scripts move or stop using kind."""
    found = _side_loaders()

    assert len(found) >= 2, f"only {[p.name for p in found]} side-load images — the scan moved, not the scripts"


@pytest.mark.parametrize("script", [p.name for p in _side_loaders()])
def test_a_side_loading_script_declares_localImages(script: str) -> None:
    body = (_ROOT / "scripts" / script).read_text(encoding="utf-8")

    assert re.search(r"--set\s+image\.localImages=true", body), (
        f"{script} loads images into the kind node and never sets image.localImages=true, so the chart "
        "refuses the render and the deploy fails after every image is already built and loaded."
    )


@pytest.mark.parametrize("script", [p.name for p in _side_loaders()])
def test_the_flag_reaches_the_upgrade_that_deploys(script: str) -> None:
    """Not merely present in the file: it has to be on the `upgrade --install` that creates the release.

    A `--set` on some other helm call would satisfy a naive grep and change nothing about the deploy.
    """
    body = (_ROOT / "scripts" / script).read_text(encoding="utf-8")
    start = body.index("upgrade --install")
    # The invocation runs to the first line that does not end in a continuation.
    lines = body[start:].splitlines()
    invocation = []
    for line in lines:
        invocation.append(line)
        if not line.rstrip().endswith("\\"):
            break

    # FOLLOW ONE LEVEL OF INDIRECTION. Both stacks now hoist their flags into a `HELM_SET` array so the
    # side-load RENDER and the upgrade cannot disagree about what is deployed, and a check that reads
    # only the invocation's own lines would call that a regression. Resolving the array keeps the
    # property this test is about — the flag reaches THIS deploy, not some other helm call — and makes
    # it stronger, because the array it resolves is the one the build loop reads too.
    text = "\n".join(invocation)
    for array in re.findall(r'"\$\{(\w+)\[@\]\}"', text):
        block = re.search(rf"^{array}=\((.*?)^\)", body, re.DOTALL | re.MULTILINE)
        assert block, f"{script} expands ${array}[@] on its upgrade but defines no such array"
        invocation.extend(block.group(1).splitlines())

    assert any("image.localImages=true" in line for line in invocation), (
        f"{script} sets image.localImages somewhere, but not on the `upgrade --install` that deploys: {invocation[:3]}"
    )
