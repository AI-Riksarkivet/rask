"""The activity span exists, is correctly parented, and says nothing.

daprd emits `activity||run_node` (SERVER) and the Python SDK emits `activity: run_node` (INTERNAL),
both correctly parented into the run's trace. Neither can carry what this service knows: the SDK's span
holds `task.instance_id`, `task.id` and `activity.name`, daprd's holds its own, and no span anywhere
names the NODE. In a run of 256 activity spans an operator cannot tell which node is slow or which kind
fails.

And neither marks failure. Measured estate-wide: 46/46 SDK activity spans STATUS_CODE_UNSET, including
runs whose nodes failed. daprd marks the ORCHESTRATION span when an activity RAISES — but `run_node`
deliberately RETURNS a failed NodeResult rather than raising (a failed node is a normal outcome of a
sandbox run that the builder paints red, and raising would retry it four times), so the exception never
reaches a `with` block anyone owns.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import respx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from flows.activities import run_node  # the @wfr.activity wrapper; __wrapped__ is the body
from flows.models import FlowNode, NodeJob


SERVE = "http://serve.test"


class _Ctx:
    """The slice of `WorkflowActivityContext` the activity reads.

    It used to read NOTHING, so these tests passed `None` — which stopped being a faithful double the
    moment the activity started deriving its idempotency key from `ctx`. A double that omits what the
    code under test uses proves the code does not use it.
    """

    workflow_id = "wf-1"
    task_id = 7

    def get_inner_context(self) -> object:
        class _Inner:
            task_execution_id = "exec-abc"

        return _Inner()


def _traced() -> tuple[InMemorySpanExporter, Any]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider.get_tracer("test")


@respx.mock
def test_a_FAILING_node_marks_the_activity_span_and_names_itself() -> None:
    """The two facts the sidecar cannot supply: the verdict of a RETURNED failure, and the node."""
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(503, text="serve is down"))
    exporter, tracer = _traced()
    job = NodeJob(node=FlowNode(id="ocr-1", kind="model", config={"app": "htrflow"}), inputs=["seed"], serve_url=SERVE)

    with tracer.start_as_current_span("activity: run_node"):
        result = run_node.__wrapped__(cast("Any", _Ctx()), job)

    assert result["state"]["status"] == "failed", "the fixture did not produce a failed node"
    (span,) = exporter.get_finished_spans()

    assert span.status.status_code is StatusCode.ERROR, (
        "a failed node leaves the activity span UNSET, so trace-based error search and any rule over span "
        "status show a clean estate. The node RETURNS failure rather than raising, so no `with` block sees "
        "an exception — the application has to say so."
    )
    assert (span.attributes or {}).get("lance.flows.node_id") == "ocr-1", (
        f"the span does not name the node: {dict(span.attributes or {})}. In a 256-span run this is the "
        "difference between 'an activity failed' and 'ocr-1 failed'."
    )
    assert (span.attributes or {}).get("lance.flows.node_kind") == "model", "the span does not carry the node kind"


@respx.mock
def test_a_SUCCEEDING_node_names_itself_but_is_not_marked_an_error() -> None:
    """The other half: attributes ride every node, the ERROR status rides only the failed ones."""
    respx.post(f"{SERVE}/htrflow").mock(return_value=httpx.Response(200, text="Anno 1723"))
    exporter, tracer = _traced()
    job = NodeJob(node=FlowNode(id="ocr-2", kind="model", config={"app": "htrflow"}), inputs=["seed"], serve_url=SERVE)

    with tracer.start_as_current_span("activity: run_node"):
        result = run_node.__wrapped__(cast("Any", _Ctx()), job)

    assert result["state"]["status"] == "succeeded"
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is not StatusCode.ERROR, "a succeeding node must not be marked an error"
    assert (span.attributes or {}).get("lance.flows.node_id") == "ocr-2"
