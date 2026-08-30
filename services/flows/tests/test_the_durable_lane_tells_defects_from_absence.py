"""A broken build and an absent sidecar are not the same event (FLOWS-BROAD-EXCEPT).

The lifespan used to wrap the `flows.workflow` import, `wfr.start()`, the seam construction and the
actor-state probe in ONE `except Exception`, and report every outcome as
`"dapr workflow runtime unavailable — runs execute inline"` at WARNING. So a `NameError`, a bad
`@wfr.activity` signature, a Pydantic model error or a missing transitive dependency inside
`flows/workflow.py` produced a service that booted GREEN and ran every flow inline forever, with one
warning line as the only evidence — the exact asymmetry `test_workflow.py`'s docstring records ingest
paying for once ("`Workflow engine started` while the app had registered nothing").

The two halves are tested apart because they must behave apart:

* **A build defect crash-loops.** The import happens outside any guard; the chart's `waitFor: []`
  lets the pod restart until the image is fixed, which is the honest report.
* **An unreachable sidecar still degrades.** The guard survives, narrowed to the connection-shaped
  errors `wfr.start()` can actually raise, so an ordering blip on a cold pod does not become a
  CrashLoopBackOff. That is the deliberate behaviour the original block existed for, and it must not
  be lost to the fix.

And the resulting lane is READABLE, so "durable expected, inline actual" is a state an operator can
observe rather than a log line they have to have grepped.
"""

import sys
from collections.abc import Sequence
from typing import cast

import pytest
from fastapi import FastAPI

from flows.config import build_flows_settings
from flows.lifespan import make_lifespan
from service_kit.config import Settings


async def _boot(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Run the lifespan to its yield and hand back the app it built."""
    app = FastAPI()
    monkeypatch.setenv("DAPR_GRPC_PORT", "50001")
    # The probe reaches the sidecar's metadata route over HTTP; there is none here, and it answers
    # False rather than raising (see `service_kit.governed.actor_state_store`).
    cm = make_lifespan(Settings())(app)
    await cm.__aenter__()
    app.state._exit = cm
    return app


@pytest.mark.asyncio
async def test_a_broken_workflow_module_crashes_the_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` in `sys.modules` is what a half-imported module leaves behind — the import raises."""
    monkeypatch.setitem(sys.modules, "flows.workflow", None)

    with pytest.raises(ImportError):
        await _boot(monkeypatch)


@pytest.mark.asyncio
async def test_a_sidecar_that_is_not_answering_still_degrades_to_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deliberate degrade, kept: a connection-shaped failure boots green on the inline lane."""
    import grpc

    from flows import runtime

    class _Unreachable(grpc.RpcError):
        pass

    def _boom() -> None:
        raise _Unreachable("failed to connect to all addresses")

    monkeypatch.setattr(runtime.wfr, "start", _boom)

    app = await _boot(monkeypatch)
    try:
        assert app.state.workflow_scheduler is None
        assert app.state.workflow_reader is None
        assert app.state.workflow_lane == "inline"
    finally:
        await app.state._exit.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_the_active_lane_is_readable_state_not_only_a_log_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """A started runtime reports `durable`; nothing has to be grepped to know which lane is live."""
    from flows import runtime

    started: list[bool] = []
    monkeypatch.setattr(runtime.wfr, "start", lambda: started.append(True))
    monkeypatch.setattr(runtime.wfr, "shutdown", lambda: None)

    app = await _boot(monkeypatch)
    try:
        assert started == [True]
        assert app.state.workflow_lane == "durable"
        assert app.state.workflow_scheduler is not None
    finally:
        await app.state._exit.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_no_sidecar_at_all_is_the_inline_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DAPR_GRPC_PORT` unset — the branch is never entered and nothing pays for grpc."""
    app = FastAPI()
    monkeypatch.delenv("DAPR_GRPC_PORT", raising=False)
    async with make_lifespan(Settings())(app):
        assert app.state.workflow_lane == "inline"
        assert app.state.workflow_scheduler is None


def test_the_settings_are_still_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard for the helper above: `build_flows_settings` is what the lifespan reads."""
    assert isinstance(build_flows_settings().serve_timeout, float)


@pytest.mark.asyncio
async def test_the_startup_lane_reaches_a_metric_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """`flows.durable_lane` is the surface an operator has WITHOUT a log: 1 durable, 0 inline.

    Collected through a real in-memory reader rather than by poking module state, because the thing
    being pinned is that the gauge is registered against the meter at all — a callback nobody wired
    up would satisfy every other assertion in this file.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])

    import opentelemetry.metrics as otel_metrics

    monkeypatch.setattr(otel_metrics, "get_meter", lambda *a, **k: provider.get_meter("lance.flows"))
    import importlib

    from flows import metrics as _metrics

    metrics = importlib.reload(_metrics)

    def _points() -> dict[str, int]:
        data = reader.get_metrics_data()
        assert data is not None, "the reader collected nothing at all"
        found: dict[str, int] = {}
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != "flows.durable_lane":
                        continue
                    # `cast`: the reader types every point as the union of number/histogram shapes;
                    # a gauge only ever yields `NumberDataPoint`, which is the one with a `value`.
                    for point in cast(Sequence[NumberDataPoint], metric.data.data_points):
                        attributes = point.attributes or {}
                        found[str(attributes["lance.flows.lane"])] = int(point.value)
        return found

    assert _points() == {"inline": 0}, "a process that never announced a lane reports the inline default"

    metrics.record_lane(metrics.DURABLE)
    assert _points() == {"durable": 1}

    metrics.record_lane(metrics.INLINE)
    assert _points() == {"inline": 0}

    # Restore the module every other test in the session imported.
    monkeypatch.undo()
    importlib.reload(metrics)
