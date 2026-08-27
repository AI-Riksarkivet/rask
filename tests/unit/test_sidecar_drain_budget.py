"""The Dapr sidecar must outlive the app's drain, not race it.

open_fastapi-audit — "The Dapr sidecar's shutdown is not bounded against the pod's drain window —
daprd's 5 s default kills the publish path the app still needs while it drains".

`rask.daprAnnotations` emits app-id, app-port, log-level, log-as-json, max-body-size,
app-token-secret, sidecar resources and dapr.io/config — and nothing about shutdown. So daprd takes
its own 5 s default while the app container is still inside its 5 s preStop sleep plus whatever drain
the grace period allows. The kubelet SIGTERMs every container in the pod SIMULTANEOUSLY, so the
sidecar is gone before the app's drain window has properly opened.

THE CONCRETE FAILURE, and it is silent. A mover or the producer that is mid-handler when a rollout
starts finishes its Lance write and then calls `publish_event` to fire the next stage against a
sidecar that has already stopped: the write landed, the cascade trigger did not, and nothing
distinguishes that from a stage that simply had no successor. The estate has already paid for this
class of mismatch once — `service_kit.dapr_publish` exists because "the bare publish_event is
unbounded, so a wedged sidecar hangs this activity forever", which bounds the hang without making the
publish succeed. It also undermines the drain gate on `/bronze-arrival`, whose RETRY verdict has to
travel back through the sidecar to reach the broker.

TWO NUMBERS, ONE FORMULA. `kubernetes.md` gives the grace budget as
`preStop + max in-flight + lifespan cleanup + buffer`, and `microservices.md` adds the sidecar term
explicitly: "+ sidecar drain time (~10 s — Dapr flushes pending acks)". The fix is not a magic
constant in an annotation — it is that the sidecar's block duration and the pod's grace period are
derived from the same values so they cannot drift.
"""

from __future__ import annotations

import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

#: Annotation daprd reads to hold its shutdown open while the app drains.
BLOCK = "dapr.io/block-shutdown-duration"


def _sidecar_pods() -> dict[str, dict[str, str]]:
    """Every rendered pod template carrying the Dapr sidecar, keyed by workload name."""
    from test_invariants import _rendered_docs  # noqa: PLC0415

    found: dict[str, dict[str, str]] = {}
    for doc in _rendered_docs("explorer.enabled=true"):
        if doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        template = doc["spec"]["template"]
        annotations = (template.get("metadata") or {}).get("annotations") or {}
        if annotations.get("dapr.io/enabled") == "true":
            found[doc["metadata"]["name"]] = annotations
    return found


def test_every_sidecar_is_told_to_outlive_the_apps_drain() -> None:
    """Without this daprd takes its 5s default and dies inside the app's own preStop sleep."""
    pods = _sidecar_pods()
    assert pods, "no Dapr-annotated pods rendered — this gate would pass vacuously"

    unbounded = sorted(name for name, ann in pods.items() if BLOCK not in ann)
    assert not unbounded, (
        f"these pods run a Dapr sidecar with no {BLOCK}: {unbounded} — daprd takes its 5s default and "
        f"stops while the app is still draining, so a mover's post-write publish_event fires at a "
        f"sidecar that is already gone"
    )


def test_the_sidecar_block_is_bounded_by_the_pods_grace_period() -> None:
    """The sidecar must outlive the app's drain and still die before SIGKILL.

    A block longer than the grace period is not "safer": the kubelet SIGKILLs the whole pod at the
    deadline, so the sidecar would be killed mid-flush having refused to start flushing earlier.
    """
    import yaml  # noqa: PLC0415
    from test_invariants import CHART  # noqa: PLC0415

    values = yaml.safe_load((CHART / "values.yaml").read_text())
    lifecycle = values["lifecycle"]
    grace = int(lifecycle["terminationGracePeriodSeconds"])
    pre_stop = int(lifecycle["preStopSeconds"])
    block = int(str(lifecycle["sidecarBlockShutdownSeconds"]).rstrip("s"))

    assert block > pre_stop, (
        f"the sidecar blocks for {block}s but the app sleeps {pre_stop}s in preStop before it even begins draining — the sidecar would still go first"
    )
    assert block < grace, f"the sidecar blocks for {block}s against a {grace}s grace period — it would be SIGKILLed mid-flush having declined to flush earlier"


def test_the_grace_period_budgets_the_sidecar_drain() -> None:
    """`microservices.md` adds a sidecar term to the no-sidecar formula: "+ sidecar drain time
    (~10 s — Dapr flushes pending acks)". A pod that runs a sidecar and keeps the un-adjusted budget
    is spending the app's drain allowance on the sidecar's."""
    import yaml  # noqa: PLC0415
    from test_invariants import CHART  # noqa: PLC0415

    lifecycle = yaml.safe_load((CHART / "values.yaml").read_text())["lifecycle"]
    grace = int(lifecycle["terminationGracePeriodSeconds"])
    block = int(str(lifecycle["sidecarBlockShutdownSeconds"]).rstrip("s"))
    pre_stop = int(lifecycle["preStopSeconds"])

    # preStop + the sidecar's own drain + a buffer must still fit, or the budget is nominal.
    assert grace >= pre_stop + block + 5, f"grace={grace}s does not cover preStop={pre_stop}s + sidecar block={block}s + a 5s buffer"


def test_the_fleet_pods_actually_declare_a_grace_period() -> None:
    """Eight templates set `lifecycle.terminationGracePeriodSeconds`; fleet.yaml and controlplane.yaml
    did not, so the six fleet pods silently took the kubelet's 30s default — a number nobody chose and
    that no values comment describes."""
    from test_invariants import _rendered_docs  # noqa: PLC0415

    naked: list[str] = []
    for doc in _rendered_docs("explorer.enabled=true"):
        if doc.get("kind") != "Deployment" or "rask-" not in doc["metadata"]["name"]:
            continue
        if "web-" in doc["metadata"]["name"]:
            continue
        if doc["spec"]["template"]["spec"].get("terminationGracePeriodSeconds") is None:
            naked.append(doc["metadata"]["name"])

    assert not naked, f"these fleet Deployments declare no terminationGracePeriodSeconds: {sorted(naked)}"
