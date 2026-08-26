"""`request_approval` is the SOLE producer of `promotion_review_requested`, and it never ran.

The reason that asks a named person to decide a held promotion had no test executing it: the
`CatalogControlEvent` construction, the `_publish()` closure and the success log were all reported
missing by coverage. Two suites appeared to cover it and each covered the other half —
`test_promotion_review.py` stubs the activity, so the orchestration around it is proven and the thing
that names the human is not.

That matters more here than for an ordinary emit, because `extra["subject"]` IS the targeting.
`.claude/skills/rask-notifications` states it for the whole control lane: "`named_subject` returns
`None` for a missing subject, a bare `user:`, and the `*` wildcard, and the event is then filed IGNORED
with a SUCCESS ack." So every way of getting this field wrong produces a healthy-looking event that
reaches nobody — there is no failure signal anywhere downstream to catch it.

The workflow's own comment states the contract this file pins: "ASK BEFORE WAITING, and treat an
unsendable ask as a refusal: parking on an event nobody was told about is an outage wearing a pause."
So the return value is load-bearing in both directions — `False` must BLOCK rather than park.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from dapr.ext.workflow import WorkflowActivityContext
from medallion import workflow as wf


class _StubActivityContext:
    """An activity context. `request_approval` never touches it — pinned by this file passing."""


def _ctx() -> WorkflowActivityContext:
    """The stub, typed as the real context.

    A `cast` rather than a subclass or a `# type: ignore`: `WorkflowActivityContext` takes a live
    workflow instance to construct, the activity provably never touches the parameter, and `ty` does
    not honour `type: ignore` anyway — it is another tool's syntax. This is the same shape
    `test_producer_targeting_contract.py` uses for its unused resolved dependencies.
    """
    return cast(WorkflowActivityContext, _StubActivityContext())


def _spec(**overrides: Any) -> dict[str, Any]:
    base = {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "silver",
        "from_dataset": "acme-silver$t",
        "to_namespace": "gold",
        "to_dataset": "catalog",
        "reasons": ["row_count_drop"],
        "approver": "alice",
        "approval_hours": 24,
    }
    return {**base, **overrides}


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what reaches the bus, without one.

    Both the client and the publish helper are imported INSIDE the activity body, so they are patched
    on their defining modules rather than on `medallion.workflow` — patching the latter would bind
    nothing and the test would pass while the real client ran.
    """
    from dapr.aio import clients as dapr_clients

    import service_kit.dapr_publish as dapr_publish

    sent: list[dict[str, Any]] = []

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def _publish(_client: object, **kwargs: Any) -> None:
        sent.append(kwargs)

    monkeypatch.setattr(dapr_clients, "DaprClient", _Client)
    monkeypatch.setattr(dapr_publish, "publish_event", _publish)
    return sent


def test_the_ask_names_the_approver_as_the_subject(published: list[dict[str, Any]]) -> None:
    """Q6's rule: `extra.subject` is the WORKER — here, the person being asked to decide."""
    assert wf.request_approval(_ctx(), wf.PromotionSpec.model_validate(_spec())) is True

    assert len(published) == 1, f"expected exactly one control event, got {len(published)}"
    event = json.loads(published[0]["data"])

    assert event["action"] == "promotion_review_requested"
    assert event["extra"]["subject"] == "user:alice", (
        "the subject is the entire targeting for the control lane; anything but `user:<sub>` is filed IGNORED with a SUCCESS ack and reaches nobody"
    )
    assert event["object_id"] == "table:acme-catalog", (
        "the object must be project-qualified: an unqualified name against tenant-qualified grants "
        "counts every recipient HIDDEN, so the audience is computed correctly and then discarded whole"
    )
    assert event["extra"]["reasons"] == ["row_count_drop"]
    assert event["extra"]["token"] == "tok-1", "the token is how the approver's decision finds this hold"


def test_the_control_topic_is_the_one_the_inbox_subscribes_to(published: list[dict[str, Any]]) -> None:
    """Publishing a correct event to the wrong topic reaches nobody, and looks identical from here."""
    from service_kit.control_events import CONTROL_TOPIC

    assert wf.request_approval(_ctx(), wf.PromotionSpec.model_validate(_spec())) is True
    assert published[0]["topic_name"] == CONTROL_TOPIC


def test_no_approver_refuses_the_ask_instead_of_publishing_one_nobody_can_answer(published: list[dict[str, Any]]) -> None:
    """`approver` empty means nobody can be asked. The spec's own comment: that BLOCKS, never promotes."""
    assert wf.request_approval(_ctx(), wf.PromotionSpec.model_validate(_spec(approver=""))) is False
    assert published == [], "an unapprovable promotion must publish nothing at all"


def test_a_failed_publish_is_a_refusal_not_a_silent_park(monkeypatch: pytest.MonkeyPatch) -> None:
    """The return value is the compensating control for a bus that did not take the message.

    If this returned True on a failed publish, the workflow would wait on `promotion_decision` for
    `approval_hours` for an ask that never left the process — the "outage wearing a pause" the
    orchestrator's comment names. Returning False routes it to BLOCKED with "no reachable approver",
    which is a visible outcome.
    """
    from dapr.aio import clients as dapr_clients

    import service_kit.dapr_publish as dapr_publish

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def _boom(_client: object, **_kwargs: Any) -> None:
        raise RuntimeError("pubsub unavailable")

    monkeypatch.setattr(dapr_clients, "DaprClient", _Client)
    monkeypatch.setattr(dapr_publish, "publish_event", _boom)

    assert wf.request_approval(_ctx(), wf.PromotionSpec.model_validate(_spec())) is False
