"""Gate 7 (R3) render proofs — Ray token auth static wiring in the chart.

The live tokenless-rejection proof runs at the cluster gates; these tests pin what
`helm template` can prove offline:

  * in every shape (in-cluster or external Ray) -> whatever references the token Secret, the render
    also creates it exactly once (the ExternalSecret that writes it from the store's `ray-auth-token`).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.chart_render import ESO_ARGS


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _helm(*set_values: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(CHART)]
    # Since auth defaults ON (2026-08-06) every render needs identity values, and ESO is a prerequisite.
    argv += [*ESO_ARGS]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    # The chart REQUIRES an image registry unless the images are side-loaded into the node
    # (`rask.image` in _helpers.tpl): a bare `<component>:<tag>` is `docker.io/library/...`
    # and ImagePullBackOffs on any real cluster. These tests render the LOCAL shape, which is
    # the side-loaded one, so they opt in the same way `make k3s-up` does.
    argv += ["--set", "image.localImages=true"]
    for value in set_values:
        argv += ["--set", value]
    return subprocess.run(argv, capture_output=True, text=True, check=check)  # noqa: S603


def _docs(rendered: str) -> list[str]:
    return rendered.split("\n---")


def _name(doc: str) -> str:
    m = re.search(r"^\s*name:\s*(\S+)", doc, re.MULTILINE)
    return m.group(1) if m else "?"


@pytest.mark.parametrize("single_tenant", [False, True], ids=["external-ray", "in-cluster-ray"])
def test_every_secretKeyRef_to_the_token_has_something_that_creates_it(single_tenant: bool) -> None:
    """The load-bearing symmetry: nothing may REFERENCE the token Secret unless the render also CREATES it.

    This is the invariant the individual gate tests keep missing, because each one checks a single
    template under a single combination. A `secretKeyRef` to an absent Secret is not a render error —
    kubelet accepts the pod and then wedges it in CreateContainerConfigError, so it fails at deploy time
    on a cluster, which is the most expensive place to find it.
    """
    args = ["ray.auth.enabled=true", f"singleTenant.enabled={str(single_tenant).lower()}"]
    docs = _docs(_helm(*args).stdout)

    referrers = {_name(d) for d in docs if "rask-ray-auth-token" in d and "secretKeyRef" in d}
    creators = [d for d in docs if re.search(r"^kind: (Secret|ExternalSecret)$", d, re.MULTILINE) and "rask-ray-auth-token" in d]
    assert referrers, "expected at least the rayClient fleet to reference the token in this shape"
    assert len(creators) == 1, f"{sorted(referrers)} reference rask-ray-auth-token but {len(creators)} manifests create it"
