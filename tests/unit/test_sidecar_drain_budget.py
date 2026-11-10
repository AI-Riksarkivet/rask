"""The Dapr sidecar must outlive the app's drain, not race it.

open_fastapi-audit — "The Dapr sidecar's shutdown is not bounded against the pod's drain window —
daprd's 5 s default kills the publish path the app still needs while it drains".

`rask.daprAnnotations` emits app-id, app-port, log-level, log-as-json, max-body-size,
app-token-secret, sidecar resources and dapr.io/config — and nothing about shutdown. So daprd takes
its own 5 s default while the app container is still inside its 5 s preStop sleep plus whatever drain
the grace period allows. The kubelet SIGTERMs every container in the pod SIMULTANEOUSLY, so the
sidecar is gone before the app's drain window has properly opened.

THE CONCRETE FAILURE, and it is silent. A catalog door that is mid-handler when a rollout
starts finishes its Lance write and then calls `publish_event` to announce it against a sidecar
that has already stopped: the write landed, the announcement did not, and nothing distinguishes that
from a write nobody subscribed to. `lakehouse.dapr_publish` bounds a wedged sidecar's hang without making the publish succeed. It
also undermines the drain gate on a delivered route, whose RETRY verdict has to travel back through
the sidecar to reach the broker.

TWO NUMBERS, ONE FORMULA. `kubernetes.md` gives the grace budget as
`preStop + max in-flight + lifespan cleanup + buffer`, and `microservices.md` adds the sidecar term
explicitly: "+ sidecar drain time (~10 s — Dapr flushes pending acks)". The fix is not a magic
constant in an annotation — it is that the sidecar's block duration and the pod's grace period are
derived from the same values so they cannot drift.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

#: Annotation daprd reads to hold its shutdown open while the app drains.
BLOCK = "dapr.io/block-shutdown-duration"


def _sidecar_pods() -> dict[str, dict[str, str]]:
    """Every rendered pod template carrying the Dapr sidecar, keyed by workload name."""
    from test_invariants import _rendered_docs  # noqa: PLC0415

    found: dict[str, dict[str, str]] = {}
    for doc in _rendered_docs():
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
        f"stops while the app is still draining, so a door's post-write publish_event fires at a "
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


def _deployment(docs: tuple[dict, ...], name: str) -> dict:
    return next(doc for doc in docs if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name)


@pytest.mark.parametrize(
    "moved",
    [
        pytest.param({}, id="the-shipped-lifecycle"),
        pytest.param(
            {"terminationGracePeriodSeconds": 61, "maintenanceTerminationGracePeriodSeconds": 97, "preStopSeconds": 3, "teardownSeconds": 7},
            id="every-lifecycle-number-moved",
        ),
    ],
)
def test_the_worker_runs_in_pods_of_its_own_whose_sidecar_outlives_its_drain_and_leaves_with_it(moved: dict[str, int]) -> None:
    """[[LH-494]]: maintenance's queue worker runs in a Deployment of its own, as a process and not a web app's lifespan,
    and the web pods keep the write-event lane and run no worker. A pass publishes through the sidecar until it ends, so
    the worker pod's sidecar blocks its shutdown for the pod's grace less the teardown, which holds preStop and the whole
    drain; and it health-checks the worker's probe port, so it leaves when the worker does rather than at the block's end
    (measured on daprd 1.19.0-rc.4: with the check, 3 s after the app; without it, the whole block). A health check also
    gates a sidecar's subscriptions, so only this pod, which subscribes to nothing, has one. The second case moves every
    lifecycle number, so only a computed grace and block pass it."""
    import yaml  # noqa: PLC0415
    from test_invariants import CHART  # noqa: PLC0415

    from tests.unit.chart_render import DEFAULT_ARGS, render, selects  # noqa: PLC0415

    lifecycle = {**yaml.safe_load((CHART / "values.yaml").read_text())["lifecycle"], **moved}
    docs = render(
        *DEFAULT_ARGS, "--set", "maintenance.queue.drainSeconds=60", *(arg for key, value in moved.items() for arg in ("--set", f"lifecycle.{key}={value}"))
    )
    worker, web = (_deployment(docs, name)["spec"]["template"] for name in ("rask-maintenance-worker", "rask-maintenance"))
    process, served = worker["spec"]["containers"][0], web["spec"]["containers"][0]
    env = {entry["name"]: entry.get("value") for entry in process["env"]}
    notes = worker["metadata"]["annotations"]
    grace = worker["spec"]["terminationGracePeriodSeconds"]
    selecting = [
        doc["metadata"]["name"]
        for doc in docs
        if doc.get("kind") == "Service" and selects({"matchLabels": doc["spec"].get("selector") or {"none": "none"}}, worker["metadata"]["labels"])
    ]

    assert process["args"][-3:] == ["python", "-m", "maintenance.worker"]
    pre_stop = int(" ".join(process["lifecycle"]["preStop"]["exec"]["command"]).rsplit(" ", 1)[-1])
    assert (grace, pre_stop, selecting) == (int(lifecycle["maintenanceTerminationGracePeriodSeconds"]), int(lifecycle["preStopSeconds"]), [])
    assert pre_stop + int(env["MAINTENANCE_DRAIN_SECONDS"]) <= int(notes[BLOCK].rstrip("s")) == grace - int(lifecycle["teardownSeconds"])
    assert (notes["dapr.io/enable-app-health-check"], notes["dapr.io/app-health-check-path"]) == ("true", "/livez")
    assert notes["dapr.io/app-port"] == str(process["ports"][0]["containerPort"]) == env["MAINTENANCE_WORKER_PORT"]
    assert (env["MAINTENANCE_ARRIVAL_ENABLED"], process["livenessProbe"]["httpGet"]["path"], process["readinessProbe"]["httpGet"]["path"]) == (
        "false",
        "/livez",
        "/readyz",
    )
    web_env = {entry["name"]: entry.get("value") for entry in served["env"]}
    assert ("MAINTENANCE_DRAIN_SECONDS" in web_env, "dapr.io/enable-app-health-check" in web["metadata"]["annotations"]) == (False, False)
    assert (web["spec"]["terminationGracePeriodSeconds"], web["metadata"]["annotations"][BLOCK]) == (
        int(lifecycle["terminationGracePeriodSeconds"]),
        f"{lifecycle['sidecarBlockShutdownSeconds']}s",
    )
