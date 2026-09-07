"""A service pinned to ONE replica for correctness must not run two during a deploy.

`open_lakehouse_diff_left.md` § H4, third clause ("`strategy: Recreate`").

`rask-maintenance` is `replicas: 1` and that is a CORRECTNESS constraint, not a capacity choice.
`bindings.cron` is stateless and uncoordinated — Diagrid, verbatim: *"No coordination – each replica
runs the schedule independently, causing duplicate triggers"* — and this service has no lease
(`records.create_json` exists and is not used here). The estate buys the guarantee four different
ways across its cron consumers, and maintenance's answer is the pin.

**A ROLLING UPDATE SPENDS EXACTLY THAT GUARANTEE.** Measured on the deployed estate 2026-09-07:

    rask-maintenance   replicas 1   strategy RollingUpdate   maxSurge 25%

Kubernetes rounds `maxSurge` UP, so 25% of one replica is one surge pod: for the length of every
rollout there are TWO maintenance pods, both with a cron binding, both able to tick. The steady-state
posture is correct and the transition violates it — which is why reading `replicas: 1` is not enough
to conclude the invariant holds.

`tests/unit/test_prod_ha_posture.py` reasons about how many replicas a service RUNS and records why
three of them may not scale. It does not reach the rollout, and this file is the other half of that:
the pin has to survive the deploy that applies it.

SCOPED TO THE CORRECTNESS PIN, deliberately. Seven other deployments hardcode `replicas: 1`
(dex, openbao, the collector, alerting, dapr-dashboard, age-postgres), and none of them was measured
to have an unsafe-concurrency constraint — none mounts a PVC either, so the RWO-volume deadlock that
usually motivates `Recreate` does not apply to them. Widening this gate to "every single-replica
deployment" would assert a constraint for six services on no evidence.
"""

from __future__ import annotations

import yaml

from tests.unit.test_invariants import _helm_template


#: The service whose single replica is a documented correctness constraint rather than a capacity one.
_PINNED_FOR_CORRECTNESS = "-maintenance"


def _deployments() -> dict[str, dict]:
    docs = [d for d in yaml.safe_load_all(_helm_template("dapr.enabled=true")) if d]
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Deployment"}


def test_the_maintenance_deployment_uses_Recreate() -> None:
    """The headline: no surge pod, so the cron never has a second host mid-rollout."""
    deployments = _deployments()
    name = next((n for n in deployments if n.endswith(_PINNED_FOR_CORRECTNESS)), None)
    assert name, f"no maintenance Deployment in the render — the gate would pass vacuously ({sorted(deployments)[:6]})"

    spec = deployments[name]["spec"]
    assert int(spec.get("replicas", 1)) == 1, (
        f"{name} is no longer pinned to one replica — if it can scale, this gate is the wrong constraint and the cron needs a real lease instead"
    )
    strategy = (spec.get("strategy") or {}).get("type")
    assert strategy == "Recreate", (
        f"{name} rolls out with strategy {strategy!r}: maxSurge rounds UP on one replica, so a deploy runs TWO "
        "pods, both holding a cron binding that fires independently — the exact duplicate-tick the single-replica "
        "pin exists to prevent"
    )


def test_a_deployment_that_CAN_scale_is_left_on_RollingUpdate() -> None:
    """The other direction, and the one that stops this becoming a blanket.

    `Recreate` means downtime on every deploy. It is the right trade only where two live pods would be
    WRONG; for a service that may run several, it turns a zero-downtime rollout into an outage.
    """
    deployments = _deployments()
    scalable = {
        name: spec for name, d in deployments.items() for spec in [d["spec"]] if not name.endswith(_PINNED_FOR_CORRECTNESS) and int(spec.get("replicas", 1)) > 1
    }
    forced = {n for n, s in scalable.items() if (s.get("strategy") or {}).get("type") == "Recreate"}
    assert not forced, f"these multi-replica deployments were forced to Recreate, trading zero-downtime for nothing: {sorted(forced)}"
