"""The OTel Collector carries a liveness probe, not only a readiness probe.

[[XC-059]] EVERY SIGNAL IN THE ESTATE FLOWS THROUGH THIS ONE POD — it receives the fleet's OTLP, tails
infra-pod logs and scrapes the Dapr sidecars. A readiness probe alone answers "should traffic go here",
and Kubernetes' response to a persistent no is to route around it. There is nothing to route around a
singleton: a wedged Collector simply stays Ready-false forever, and every trace, metric and log in the
estate stops with nothing restarting the pod.

A LIVENESS PROBE IS THE ONLY THING THAT RESTARTS IT, which is the distinction that matters here and is
easy to lose because both probes hit the same endpoint. Readiness gates traffic; liveness gates the
container's life. The `health_check` extension is already configured on `0.0.0.0:13133`, so this costs
no new surface — only the second probe that acts on the answer.

THE TWO PROBES ARE ASSERTED TOGETHER, because deleting the readiness probe to "simplify" would leave a
Collector that restarts but is never gated during startup, and the row asks for both.
"""

from __future__ import annotations

from tests.unit.chart_render import DEFAULT_ARGS, containers, render


def _collector() -> dict:
    found = [container for where, name, container in containers(render(*DEFAULT_ARGS)) if "otel-collector" in where and name != "daprd"]
    assert found, "no otel-collector container rendered; this gate would check nothing"
    return found[0]


def test_the_collector_is_ready_gated() -> None:
    """The half that already existed — asserted so removing it is a failure, not a silent regression."""
    assert _collector().get("readinessProbe"), "the Collector has no readiness probe, so traffic reaches it before it can serve"


def test_the_collector_can_be_restarted_when_it_wedges() -> None:
    """RED before the fix: readiness only, so a wedged singleton was never restarted."""
    probe = _collector().get("livenessProbe")

    assert probe, "the Collector has no liveness probe; a wedged one stays Ready-false forever and every signal in the estate stops"
    assert probe.get("httpGet", {}).get("port") == 13133, f"the liveness probe must hit the `health_check` extension's port, got {probe.get('httpGet')}"


def test_liveness_is_slacker_than_readiness() -> None:
    """A liveness probe that fires before readiness turns a slow start into a CRASH LOOP.

    Restarting is the heavier action, so it must be the later one: `initialDelaySeconds` at least as
    generous, and a failure threshold that does not kill the pod on a single blip while it is still
    coming up. This is the trap that makes liveness probes net-negative when they are added carelessly.
    """
    container = _collector()
    live, ready = container["livenessProbe"], container["readinessProbe"]

    assert live.get("initialDelaySeconds", 0) >= ready.get("initialDelaySeconds", 0), (
        f"liveness starts at {live.get('initialDelaySeconds')}s and readiness at {ready.get('initialDelaySeconds')}s; "
        "a liveness probe that fires first restarts a pod that is merely still starting"
    )
    assert live.get("failureThreshold", 1) >= 3, f"failureThreshold {live.get('failureThreshold')} restarts the estate's telemetry hub on a single blip"
