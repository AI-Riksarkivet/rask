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

from flows.activities import _idempotency_key


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


def test_the_FALLBACK_covers_the_SDKs_empty_default() -> None:
    """`task_execution_id` defaults to `''`. Sending nothing would be worse than sending a key that is
    stable within an attempt, which is what the composite is."""
    assert _idempotency_key(cast("Any", _Ctx(""))) == "wf-1:7"
    assert _idempotency_key(cast("Any", _Ctx(None))) == "wf-1:7"
