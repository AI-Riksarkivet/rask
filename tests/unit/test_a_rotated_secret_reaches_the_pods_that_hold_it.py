"""A pod that consumes a Secret through `secretKeyRef` must be rolled when that Secret changes.

the lakehouse register, row E7 (drained 2026-09-10; in git history). MEASURED LIVE 2026-09-08 and fixed the same day: an env value from
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


#: Workloads that consume a Secret and do NOT yet hash it into their pod template. Named rather than
#: excluded silently: each is infrastructure rendered by its own template (a subchart shape or a
#: StatefulSet), so covering them is a separate change — and an empty list here would read as "all
#: covered", which is the kind of quiet gap this whole test exists to prevent.
_UNCOVERED = frozenset({"rask-age", "rask-greptimedb-standalone", "rask-openfga", "rask-otel-collector"})


def test_no_lakehouse_workload_consumes_a_secret_it_would_not_be_rolled_for() -> None:
    """The general rule, not just the zones — a rotation must reach EVERY pod holding the credential.

    Measured 2026-09-08 before this landed: 14 of the 22 workloads that consume a Secret had no hash of
    it, including the whole lakehouse plane (catalog, lineage, maintenance, the producer and all three
    stage runners) and `rask-age`, which holds the database password.
    """
    docs = _render().split("\n---\n")
    offenders: list[str] = []
    for doc in docs:
        if not re.search(r"^kind: (Deployment|StatefulSet)$", doc, re.MULTILINE):
            continue
        named = re.search(r"^  name: (\S+)$", doc, re.MULTILINE)
        name = named.group(1) if named else "<unnamed>"
        secrets = {s.strip('"') for s in re.findall(r"secretRef:\s*\n\s*name: (\S+)", doc)}
        secrets |= {s.strip('"') for s in re.findall(r"secretKeyRef:\s*\n?\s*(?:\{\s*)?name: (\S+)", doc)}
        if not {s for s in secrets if s} or name in _UNCOVERED:
            continue
        # `checksum/config` is the ConfigMap's and does not stand in for a Secret's.
        if not {c for c in re.findall(r"checksum/(\S+):", doc) if c != "config"}:
            offenders.append(name)

    assert not offenders, f"consume a Secret with no checksum for it, so a rotation would not reach them: {sorted(offenders)}"


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
