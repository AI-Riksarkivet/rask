"""A service starts a saga through the port, not by importing the engine.

THE ESTATE HAS TWO STACKED SEAMS: a workflow engine (durable steps, timers, external events) in front
of a compute engine (`Executor`, `WorkOrder`, `task_registry`). Ingest, batch processing and the
quality gate are that pair instantiated three times, not three planes. The compute half had a port;
the workflow half did not.

WHAT WAS ACTUALLY MISSING was narrower than an import count suggests, and measuring it is what made
the fix small. 68 orchestration constructs live in FOUR files, two carrying 65 — `medallion/workflow.py`
and `ingest/workflow.py`. The other ten files importing `dapr.ext.workflow` carry ZERO orchestration:
they register runtimes and call clients. Orchestration is engine-SHAPED by nature (Dapr replays a
generator, Argo walks a YAML DAG, Flyte composes decorated tasks) and does not reduce to one interface
without becoming the lowest common denominator of three.

What DOES reduce is the ACTIVITY layer's reach upward: `transform.py` and `train.py` each imported
`dapr.ext.workflow` INSIDE a function body purely to start an instance. A service that has just built
a `WorkOrder` should not have to know which engine will carry it.

This gate pins the seam rather than the count: the two activity modules name no engine, and the
adapter that does is the only one that may.
"""

from __future__ import annotations

from pathlib import Path

from service_kit.lakehouse.saga import SagaClient, SagaHandle, SagaStart


REPO = Path(__file__).resolve().parents[2]

#: Modules that BUILD work and hand it to a saga. They may not name an engine.
_ACTIVITY_MODULES = (
    "services/medallion/src/medallion/services/transform.py",
    "services/medallion/src/medallion/services/train.py",
)

#: The one module allowed to name Dapr Workflow on the starting path — the adapter itself.
_ADAPTER = "services/medallion/src/medallion/services/dapr_saga.py"


def test_the_activity_modules_name_no_workflow_engine() -> None:
    for rel in _ACTIVITY_MODULES:
        text = (REPO / rel).read_text()
        assert "dapr.ext.workflow" not in text, (
            f"{rel} imports the workflow engine to start a saga — that is the seam `saga.SagaClient` "
            f"exists to hold, and it is what makes 'bring your own workflow engine' untrue"
        )


def test_the_adapter_is_the_one_place_the_engine_is_named() -> None:
    """A port with no adapter is a description; the adapter has to exist and has to be the seam."""
    text = (REPO / _ADAPTER).read_text()
    assert "dapr.ext.workflow" in text, "the Dapr adapter no longer names Dapr — the port has no implementation"


def test_the_dapr_adapter_satisfies_the_port() -> None:
    """Structurally, not by inheritance — the same shape `Executor`'s adapters use, so the port stays a
    description of behaviour rather than a base class services must derive from."""
    from medallion.services.dapr_saga import DaprSagaClient

    assert isinstance(DaprSagaClient(), SagaClient)


def test_already_running_is_a_success_the_port_can_express() -> None:
    """The distinction the medallion used to re-derive by hand: a schedule failure is two events
    wearing one exception, and "already watched" must be tellable from "nothing is watching"."""
    handle = SagaHandle(instance_id="stage-abc", outcome=SagaStart.ALREADY_RUNNING)
    assert handle.outcome is not SagaStart.STARTED
    assert set(SagaStart) == {SagaStart.STARTED, SagaStart.ALREADY_RUNNING}


def test_the_port_adds_no_engine_dependency_to_service_kit() -> None:
    """`service-kit` must not gain a workflow-engine dependency — the same rule `executor.py` holds for
    `ray`. A port that imports the thing it abstracts is not a port."""
    text = (REPO / "packages/service-kit/src/service_kit/lakehouse/saga.py").read_text()
    for engine in ("dapr", "argo", "flyte", "temporal"):
        assert f"import {engine}" not in text, f"the saga port imports {engine}"
