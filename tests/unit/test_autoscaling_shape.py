"""If autoscaling is ever switched on, the object it renders has to be able to work.

open_fastapi-audit — "The HPA scales the two most I/O-bound services on CPU utilization against a 50m
request with a 1000m limit, and has no scale-down stabilization window".

OPT-IN AND OFF EVERYWHERE, which is why the finding grades this low and why the fix stops where it
does. Owner decision 2026-08-28: fix the SHAPE on CPU rather than add a twelfth operator. The
reference's preferred form — a Pods-type p95 target — was costed and declined: KEDA's prometheus
scaler would point at `http://rask-greptimedb-standalone:4000/v1/prometheus`, the same endpoint
vmalert already queries, so the query path is proven; but it is a new subchart, new CRDs and a new
silent failure mode (scaler cannot reach GreptimeDB → no scaling, no error) for an object nothing
enables. CPU stays, documented as the FALLBACK the reference calls it, not as the intended signal.

Two things are then required for the fallback to behave, and neither was present:

**Utilization is measured against the REQUEST, not the limit.** catalog and lineage name no
`resources` tier, so both inherit `default` — 50m request against a 1000m limit, a 20:1 burst ratio.
`averageUtilization: 70` therefore means 35 millicores: any real request load clears it instantly,
the HPA jumps `minReplicas` → `maxReplicas` on the first burst, and oscillates as the burst passes.

**Nothing damps the way back down.** The reference's own worked YAML carries
`behavior.scaleDown.stabilizationWindowSeconds: 300` precisely to stop that churn, and the template
had no `behavior` block at all.

NOT TESTED HERE, and said rather than left implied: that the values comment now names CPU as the
fallback. That is prose, and a test asserting the presence of a sentence is the brittleness this drain
keeps deleting — `test_setup_logging_installs_the_filter` was rewritten this same week for exactly
that. The comment is in `chart/values.yaml`; what is gated is the behaviour it describes.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


#: `behavior.scaleDown.stabilizationWindowSeconds` from the reference's worked example. A shorter
#: window is the churn this exists to stop; a longer one is a deployment's choice, not a defect.
MIN_SCALEDOWN_WINDOW = 300

#: Request:limit ceiling for a pod under a utilization-target HPA. The reference does not name a
#: number; what it names is the failure — utilization is a fraction of REQUEST, so a request far below
#: the limit makes the target fire on a load the pod absorbs without noticing. 4x keeps burst headroom
#: while leaving the percentage meaningful; 20x does not.
MAX_REQUEST_TO_LIMIT_RATIO = 4


def _millicores(value: str | int | float) -> int:
    text = str(value)
    return int(float(text[:-1])) if text.endswith("m") else int(float(text) * 1000)


_DOCS = _rendered_docs("autoscaling.enabled=true")
_HPAS = [d for d in _DOCS if d.get("kind") == "HorizontalPodAutoscaler"]
_DEPLOYMENTS = {d["metadata"]["name"]: d for d in _DOCS if d.get("kind") == "Deployment"}

assert _HPAS, "no HorizontalPodAutoscaler rendered with autoscaling.enabled=true — this file would pass vacuously"


@pytest.mark.parametrize("hpa", _HPAS, ids=[h["metadata"]["name"] for h in _HPAS])
def test_scale_down_is_damped(hpa: dict) -> None:
    """Without a window the HPA scales back the moment a burst passes, then up again on the next one."""
    window = ((hpa["spec"].get("behavior") or {}).get("scaleDown") or {}).get("stabilizationWindowSeconds")
    assert window is not None, (
        f"{hpa['metadata']['name']} has no behavior.scaleDown.stabilizationWindowSeconds, so it churns "
        "replicas on every burst — the reference's own worked example carries a 5-minute cooldown"
    )
    assert window >= MIN_SCALEDOWN_WINDOW, f"{hpa['metadata']['name']} cools down in {window}s, below the reference's {MIN_SCALEDOWN_WINDOW}s"


@pytest.mark.parametrize("hpa", _HPAS, ids=[h["metadata"]["name"] for h in _HPAS])
def test_the_utilization_target_is_measured_against_a_MEANINGFUL_request(hpa: dict) -> None:
    """A utilization target is a fraction of the request; a 20:1 burst ratio makes it noise."""
    if not any(m.get("type") == "Resource" and m["resource"]["name"] == "cpu" for m in hpa["spec"]["metrics"]):
        pytest.skip(f"{hpa['metadata']['name']} does not target CPU utilization")

    target = _DEPLOYMENTS.get(hpa["spec"]["scaleTargetRef"]["name"])
    assert target is not None, f"{hpa['metadata']['name']} scales a Deployment that does not render: {hpa['spec']['scaleTargetRef']['name']}"

    container = target["spec"]["template"]["spec"]["containers"][0]
    resources = container.get("resources") or {}
    request = _millicores((resources.get("requests") or {}).get("cpu", 0))
    limit = _millicores((resources.get("limits") or {}).get("cpu", 0))
    assert request, f"{container['name']} declares no CPU request, so a utilization target has no denominator"

    ratio = limit / request
    assert ratio <= MAX_REQUEST_TO_LIMIT_RATIO, (
        f"{container['name']} requests {request}m against a {limit}m limit ({ratio:.0f}:1), so the HPA's "
        f"utilization target fires at {request * 0.7:.0f}m — a load this pod absorbs without noticing, "
        "and it will slam to maxReplicas on the first burst"
    )


@pytest.mark.parametrize("hpa", _HPAS, ids=[h["metadata"]["name"] for h in _HPAS])
def test_the_deployment_replica_count_does_not_fight_the_hpa_floor(hpa: dict) -> None:
    """`ha.yaml` states this rule in a comment and nothing checked it.

    A Deployment whose `replicas:` exceeds `minReplicas` is immediately scaled DOWN by the HPA on
    every `helm upgrade`, so the chart and the autoscaler disagree about the floor forever.
    """
    target = _DEPLOYMENTS[hpa["spec"]["scaleTargetRef"]["name"]]
    replicas = int(target["spec"].get("replicas", 1))
    assert replicas <= hpa["spec"]["minReplicas"], (
        f"{target['metadata']['name']} declares {replicas} replicas but the HPA floor is "
        f"{hpa['spec']['minReplicas']} — helm sets one and the autoscaler immediately undoes it"
    )
