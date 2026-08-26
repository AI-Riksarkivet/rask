"""The retried Serve POST carried nothing to dedupe on, while `ctx` held the value that would.

DWF-ACT-002. `run_node` is retried by NODE_RETRY (3 attempts), and the outbound POST to Ray Serve IS
the side effect: a model that charges, writes, or enqueues sees the same work twice with no way to
tell them apart. The activity accepted `ctx` and never read it.

THE AUDIT'S PROPOSED KEY WOULD NOT HAVE WORKED, and this is the whole subtlety. It suggested
`f"{workflow_id}:{task_id}"`. The SDK's retry re-schedules with `id=None  # Get a new sequence
number` while passing `task_execution_id` through unchanged (`_durabletask/worker.py`), so the task
id CHANGES on exactly the event the key exists for. `task_execution_id` is the stable one; the
composite is kept only as a fallback for the SDK's `''` default.

The header spelling is the conventional `Idempotency-Key`, so a Serve app that honours it needs no
rask-specific contract and one that ignores it is no worse off than before.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from flows.activities import _idempotency_key
from flows.executor import dispatch
from flows.models import FlowNode, Payload


class _Inner:
    """The SDK's `ActivityContext`, as far as the key helper reads it."""

    def __init__(self, execution_id: str) -> None:
        self.task_execution_id = execution_id


class _NoExecutionId:
    """An older inner context that does not carry the field at all — the `getattr` default's case."""


class _Ctx:
    workflow_id = "wf-1"
    task_id = 7

    def __init__(self, execution_id: str | None) -> None:
        self._execution_id = execution_id

    def get_inner_context(self) -> Any:
        return _NoExecutionId() if self._execution_id is None else _Inner(self._execution_id)


def test_the_key_is_the_TASK_EXECUTION_id_which_survives_a_retry() -> None:
    """The correction the audit's own proposal needed."""
    assert _idempotency_key(cast("Any", _Ctx("exec-abc"))) == "exec-abc"


def test_a_retry_with_a_NEW_task_id_keeps_the_SAME_key() -> None:
    """Modelled directly: the retry re-schedules with a new sequence number and the same execution id,
    so a key built from `task_id` would change on the one event it exists for."""
    first = _Ctx("exec-abc")
    retry = _Ctx("exec-abc")
    retry.task_id = 41  # the new sequence number the SDK assigns

    assert _idempotency_key(cast("Any", first)) == _idempotency_key(cast("Any", retry))


def test_the_FALLBACK_covers_the_SDKs_empty_default() -> None:
    """`task_execution_id` defaults to `''`. Sending nothing would be worse than sending a key that is
    stable within an attempt, which is what the composite is."""
    assert _idempotency_key(cast("Any", _Ctx(""))) == "wf-1:7"
    assert _idempotency_key(cast("Any", _Ctx(None))) == "wf-1:7"


@pytest.mark.asyncio
async def test_the_key_REACHES_the_Serve_POST() -> None:
    """The assertion the whole finding turns on: a key nothing sends is not a key."""
    sent: dict[str, Any] = {}

    class _Client:
        async def post(self, url: str, **kwargs: Any) -> Any:
            sent.update(kwargs)

            class _Resp:
                status_code = 200
                text = "ok"

            return _Resp()

    node = FlowNode(id="ocr-1", kind="model", config={"app": "demo"})
    await dispatch(node, [Payload(text="in")], None, client=cast("Any", _Client()), serve_url="http://serve", idempotency_key="exec-abc")

    assert sent["headers"].get("Idempotency-Key") == "exec-abc", f"the retried POST carries nothing to dedupe on: {sent.get('headers')}"


@pytest.mark.asyncio
async def test_NO_key_sends_no_header_rather_than_an_empty_one() -> None:
    """An empty `Idempotency-Key` is worse than none: a server honouring the header would treat every
    keyless call as the same request."""
    sent: dict[str, Any] = {}

    class _Client:
        async def post(self, url: str, **kwargs: Any) -> Any:
            sent.update(kwargs)

            class _Resp:
                status_code = 200
                text = "ok"

            return _Resp()

    node = FlowNode(id="ocr-1", kind="model", config={"app": "demo"})
    await dispatch(node, [Payload(text="in")], None, client=cast("Any", _Client()), serve_url="http://serve")

    assert "Idempotency-Key" not in sent["headers"]
