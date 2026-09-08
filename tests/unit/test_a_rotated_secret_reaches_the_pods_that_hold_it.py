"""A pod that consumes a Secret through `secretKeyRef` must be rolled when that Secret changes.

`open_lakehouse_diff_left.md` § E7. MEASURED LIVE 2026-09-08 and fixed the same day: an env value from
a `secretKeyRef` is injected at pod CREATION and never refreshed, so a rotated Secret leaves its
consumers holding a dead credential while the render stays correct, the reference stays correct, and
nothing anywhere reports a problem.

    zone   LINEAGE_SERVICE_TOKEN         1b55ba766c3e962c   <- matched NO key in the Secret
    secret service-token-service-web     482611fb9ab8c057

Six of the seven zones were stale on that value. Every lineage read answered `the presented credential
may not claim 'service-web'` — **2,627 x 401, 46% of all traffic to the service** — and the symptom was
an empty panel, which is also what an idle estate looks like. A `rollout restart` fixed it; nothing
stopped the next rotation doing it again.

The standard Helm answer is to hash the Secret into the pod template: a rotation changes the
annotation, which changes the template, which rolls the consumers. This pins that the zones carry it
AND that it actually tracks the Secret's content — an annotation with a constant value would satisfy
the first check and none of the purpose.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"
_HELM_FLAGS = [
    "--set",
    "image.localImages=true",
    "--set-string",
    "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
    "--set-string",
    "frontend.oidc.publicIssuer=http://localhost:8080/dex",
    "--set-string",
    "frontend.oidc.publicOrigin=http://localhost:8080",
]


def _render(*extra: str) -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(CHART), *_HELM_FLAGS, *extra]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def test_every_zone_carries_the_infra_credentials_checksum() -> None:
    """All seven zones consume `service-token-service-web` from that Secret, so all seven must roll."""
    checksums = re.findall(r"checksum/infra-credentials: (\S+)", _render())

    assert len(checksums) == 7, f"expected one per zone, found {len(checksums)}"


def test_the_checksum_TRACKS_the_secret_rather_than_being_a_constant() -> None:
    """The half that makes the first test mean something.

    A literal would pin identically and roll nothing. Rotating a value the Secret is built from must
    move the annotation, or a credential change still fails to reach the pods holding it.
    """
    baseline = re.findall(r"checksum/infra-credentials: (\S+)", _render())[0]
    rotated = re.findall(
        r"checksum/infra-credentials: (\S+)",
        _render("--set-string", "age.password=a-different-value-entirely"),
    )[0]

    assert baseline != rotated, "the checksum does not follow the Secret — a rotation would roll nothing"
