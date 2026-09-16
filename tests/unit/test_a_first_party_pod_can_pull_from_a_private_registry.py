"""A pod running a FIRST-PARTY image must be able to pull it from a private registry.

[[XC-032]]. `imagePullSecrets` is plumbed through `.Values.imagePullSecrets` and applied in three
templates. Every other pod spec ignores it, so on any cluster whose registry needs credentials those
pods ImagePullBackOff while the three that were wired come up — a half-deployable chart, and the
failure appears as an infrastructure problem rather than as a missing block.

SCOPED TO FIRST-PARTY IMAGES, and the row's own count is why that matters. It says the block reaches
"3 of 56 templates", which is true and reads as 53 gaps; measured 2026-09-16, only SIX templates render
an image through the chart's own helpers (`lance.catalogImage`, `rask.image`) without the block —
bootstrap-admin, explorer, maintenance-worker, maintenance, medallion, services. The remaining pod
specs run PINNED third-party images (busybox, minio, dex, openbao, otel-collector, greptimedb,
postgres) from public registries. Those would need a pull secret only on a fully mirrored or airgapped
cluster, which is a larger question with a different answer — mirroring every upstream image — and
folding it in here would make this row unclosable.

Derived from the templates rather than listed, so a seventh first-party pod spec inherits the rule
instead of relearning it. Read off the SOURCE: a render under one set of values cannot distinguish a
conditional pod spec that was skipped from one that is compliant.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


CHART = Path(__file__).resolve().parents[2] / "chart" / "templates"

#: An image resolved through the chart's OWN helpers — i.e. one this estate builds and pushes, as
#: opposed to a pinned upstream tag.
_FIRST_PARTY_IMAGE = re.compile(r'include "(lance\.catalogImage|rask\.image|lance\.\w*[Ii]mage)"')
_HAS_POD_SPEC = re.compile(r"^\s+containers:", re.MULTILINE)


def _first_party_pod_templates() -> list[Path]:
    out = []
    for path in sorted(CHART.glob("*.yaml")):
        body = path.read_text(encoding="utf-8")
        if _HAS_POD_SPEC.search(body) and _FIRST_PARTY_IMAGE.search(body):
            out.append(path)
    return out


def test_the_chart_still_renders_first_party_pods() -> None:
    """Without this the parametrized assertion below would pass by iterating nothing."""
    found = _first_party_pod_templates()

    assert len(found) >= 6, f"expected the first-party pod templates, found {[p.name for p in found]}"


@pytest.mark.parametrize("template", _first_party_pod_templates(), ids=lambda p: p.name)
def test_a_first_party_pod_spec_carries_image_pull_secrets(template: Path) -> None:
    """The value exists and is threaded; what is missing is the block that consumes it."""
    body = template.read_text(encoding="utf-8")

    assert "imagePullSecrets" in body, (
        f"{template.name} renders a first-party image but no `imagePullSecrets` block, so on a cluster "
        "with a credentialed registry its pods ImagePullBackOff while the wired ones start — which reads "
        "as an infrastructure fault, not a chart gap"
    )
