"""Every uvicorn the chart launches ends its drain before the kubelet's SIGKILL ([[LH-183]]).

After SIGTERM uvicorn stops accepting, waits for the requests in flight, and only then runs the lifespan's
`finally`. `timeout_graceful_shutdown` defaults to `None` (uvicorn 0.51.0, `config.py:230`), so that wait
has no end: one slow request holds the process until SIGKILL at `terminationGracePeriodSeconds`, and the
teardown never runs. The chart renders `--timeout-graceful-shutdown` from the `lifecycle` block that also
sets each pod's grace period and preStop sleep:

    bound = the pod's terminationGracePeriodSeconds - its preStop sleep - lifecycle.teardownSeconds

READ FROM THE RENDER, per container, because each failure this exists for is only visible there: a
template whose bound names another pod's grace, a literal that matches today's defaults, and a uvicorn the
helper never reached. The second overlay moves every lifecycle number, so only a computed bound passes it.
What uvicorn does with the flag once the drain has handed it SIGTERM is proven against a real process in
`packages/service-kit/tests/test_sigterm_still_stops_the_server.py`.
"""

from __future__ import annotations

import re
import subprocess

import pytest
import yaml

from tests.unit.chart_render import DEFAULT_ARGS, REPO, containers, render, render_text


_FLAG = "--timeout-graceful-shutdown="

#: Workloads whose lifespan arms `service_kit.draining.arm_drain_on_sigterm`, so the drain hands SIGTERM to
#: THIS uvicorn. A floor: a detection that stopped finding uvicorn must not pass by finding nothing. The
#: stage runners are one Deployment per configured stage, so they are matched by container name instead.
_DRAIN_ARMED = frozenset(
    {
        "Deployment/rask-annotator",
        "Deployment/rask-gateway",
        "Deployment/rask-lineage",
        "Deployment/rask-maintenance",
        "Deployment/rask-maintenance-worker",
        "Deployment/rask-medallion-producer",
        "Deployment/rask-notifications",
        "Deployment/rask-search",
        "Deployment/rask-viewer",
    }
)

_DEFAULTS = yaml.safe_load((REPO / "chart" / "values.yaml").read_text(encoding="utf-8"))["lifecycle"]

#: Every lifecycle number moved off its default, the two grace periods apart from each other.
_MOVED = {"terminationGracePeriodSeconds": 61, "maintenanceTerminationGracePeriodSeconds": 97, "preStopSeconds": 3, "teardownSeconds": 7}


def _overlay(lifecycle: dict[str, int | str]) -> tuple[str, ...]:
    return (*DEFAULT_ARGS, "--set", "explorer.enabled=true", *(arg for key, value in lifecycle.items() for arg in ("--set", f"lifecycle.{key}={value}")))


def _bounded(docs: tuple[dict, ...]) -> dict[tuple[str, str], tuple[int, int, list[str]]]:
    """`(workload, container) -> (pod grace, preStop sleep, --timeout-graceful-shutdown args)` for every uvicorn."""
    grace = {
        f"{doc['kind']}/{doc['metadata']['name']}": doc["spec"]["template"]["spec"].get("terminationGracePeriodSeconds")
        for doc in docs
        if doc.get("kind") == "Deployment"
    }
    found: dict[tuple[str, str], tuple[int, int, list[str]]] = {}
    for workload, name, container in containers(docs):
        if not any("uvicorn" in part for part in container.get("command") or []):
            continue
        pre_stop = " ".join(container.get("lifecycle", {}).get("preStop", {}).get("exec", {}).get("command", []))
        sleep = re.search(r"sleep (\d+)", pre_stop)
        assert sleep, f"{workload}/{name} runs uvicorn with no preStop sleep to budget: {pre_stop!r}"
        found[workload, name] = (grace[workload], int(sleep.group(1)), [arg for arg in container.get("args") or [] if arg.startswith(_FLAG)])
    return found


@pytest.mark.parametrize("lifecycle", [{}, _MOVED], ids=["defaults", "every-lifecycle-number-moved"])
def test_every_uvicorn_drains_inside_its_own_pods_grace_period(lifecycle: dict[str, int | str]) -> None:
    if "teardownSeconds" not in lifecycle:
        assert "teardownSeconds" in _DEFAULTS, "values.yaml's lifecycle block budgets no teardownSeconds for the lifespan after uvicorn's drain"
    teardown = int(lifecycle["teardownSeconds"] if "teardownSeconds" in lifecycle else _DEFAULTS["teardownSeconds"])
    bounded = _bounded(render(*_overlay(lifecycle)))

    missing = sorted(_DRAIN_ARMED - {workload for workload, _ in bounded})
    assert not missing, f"{missing} arm the SIGTERM drain but no uvicorn container was found in them"
    assert any(name == "stage-runner" for _, name in bounded), "the stage runners arm the drain, and no stage-runner uvicorn was found"

    wrong = {
        f"{workload}/{name}": f"grace {grace}s, preStop {pre_stop}s, args {args}"
        for (workload, name), (grace, pre_stop, args) in bounded.items()
        if args != [f"{_FLAG}{grace - pre_stop - teardown}"]
    }
    assert not wrong, (
        f"expected exactly one {_FLAG}<grace - preStop - {teardown}s teardown> per uvicorn, derived from its own pod: {wrong}. "
        "Without it one slow request holds the process until SIGKILL and the lifespan teardown never runs."
    )
    assert all(grace - pre_stop - teardown > 0 for grace, pre_stop, _ in bounded.values())


_OUT_OF_BUDGET = "lifecycle: need 0 <= teardownSeconds < grace - preStopSeconds"


@pytest.mark.parametrize(
    ("lifecycle", "refusal"),
    [({"teardownSeconds": 35}, _OUT_OF_BUDGET), ({"teardownSeconds": -1}, _OUT_OF_BUDGET), ({"teardownSeconds": "null"}, "lifecycle.teardownSeconds")],
    ids=["zero-bound", "negative-teardown", "teardown-unset"],
)
def test_a_lifecycle_that_leaves_no_drain_refuses_to_RENDER(lifecycle: dict[str, int | str], refusal: str) -> None:
    """A bound of zero (40 - 5 - 35 on the 40s pods) makes uvicorn cancel every request in flight at
    SIGTERM. A negative teardown renders a bound that ends after the kubelet's SIGKILL, so the teardown
    it budgets for never runs. An unset teardown (an upgrade that reused a release older than the key)
    silently spends the teardown's time on requests. All three must fail the render, not the rollout."""
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*_overlay(lifecycle))
    assert refusal in refused.value.stderr, refused.value.stderr
