"""Reading a held promotion is authorized, not public (DWF-MGT sweep, 2026-08-25).

`GET /promotions/{instance_id}` answered `if gate is not None and subject:` — so a caller with NO
credential resolved `subject=None`, fell straight past `can_promote`, and got a 200 carrying the
promotion's `project`, `from_dataset`, `to_dataset`, the failed quality assertions in `reasons`, and
`approval_hours`. The route is reachable: the gateway row `("/api/promotions", "/promotions", …)` is
root-mounted and the router declares no dependencies of its own.

Two things made it worse than a read leak. The 200-vs-404 split is an oracle for WHICH reviews are
live, and its own sibling on the same router already refuses exactly this caller — "a promotion
decision must name the person who made it". `authenticate_subject`'s docstring states the contract
for both: "A caller with no verified identity gets `None` and the door refuses." `decide` honoured
it; `show` did not.

The auth-OFF posture is deliberately unchanged: with no FGA client on `app.state`, `_fga_gate`
returns `None` and the route stays open, exactly as every other dev-open door in this service.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from dapr.ext.workflow.workflow_state import WorkflowStatus
from lance_namespace import PermissionDeniedError

from medallion.api.promotions import instance_for, show


class _State:
    def __init__(self, payload: dict[str, Any], status: WorkflowStatus) -> None:
        self.serialized_input = json.dumps(payload)
        self.runtime_status = status


class _WorkflowClient:
    def __init__(self, instances: dict[str, _State]) -> None:
        self._instances = instances

    def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> _State | None:
        return self._instances.get(instance_id)


class _App:
    def __init__(self, state: Any) -> None:
        self.state = state


class _State_:
    """`app.state` stand-in — attribute access is all the route uses."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _Request:
    def __init__(self, app: _App) -> None:
        self.app = app


def _held() -> dict[str, Any]:
    return {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "silver",
        "from_dataset": "silver$features",
        "to_namespace": "gold",
        "to_dataset": "gold$catalog",
        "pub_topic": "",
        "reasons": ["row_delta_band"],
        "approver": "CiQwOGE4Njg0Yi1kYjg4",
        "originator": "CiQwOGE4Njg0Yi1kYjg4",
        "approval_hours": 72,
    }


class _FGA:
    """Enough of the client for `fga.check` to be reached; the checker itself is monkeypatched."""


def _request(*, fga_client: Any) -> _Request:
    instances = {instance_for("tok-1"): _State(_held(), WorkflowStatus.RUNNING)}
    return _Request(_App(_State_(workflow_client=_WorkflowClient(instances), fga=fga_client)))


@pytest.mark.asyncio
async def test_an_UNAUTHENTICATED_read_is_refused_when_authorization_is_ON() -> None:
    """The wedge. `subject=None` used to skip the gate rather than fail it."""
    with pytest.raises(PermissionDeniedError):
        await show(instance_for("tok-1"), _request(fga_client=_FGA()), None)  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_a_PERMITTED_reader_still_sees_the_promotion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half the fix must not break: an approver cannot answer what they cannot read."""
    seen: list[tuple[str, str]] = []

    async def _check(_client: Any, *, user: str, relation: str, obj: str) -> bool:
        seen.append((user, obj))
        return True

    monkeypatch.setattr("medallion.api.promotions.fga.check", _check)

    out = await show(instance_for("tok-1"), _request(fga_client=_FGA()), "CiQwOGE4Njg0Yi1kYjg4")  # ty: ignore[invalid-argument-type]

    assert out.to_dataset == "gold$catalog"
    assert out.reasons == ["row_delta_band"]
    assert seen == [("CiQwOGE4Njg0Yi1kYjg4", "namespace:acme-gold")], "the gate must run against the promotion's OWN destination"


@pytest.mark.asyncio
async def test_a_DENIED_reader_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _check(_client: Any, *, user: str, relation: str, obj: str) -> bool:
        return False

    monkeypatch.setattr("medallion.api.promotions.fga.check", _check)

    with pytest.raises(PermissionDeniedError):
        await show(instance_for("tok-1"), _request(fga_client=_FGA()), "CiQwOGE4Njg0Yi1kYjg4")  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_the_dev_open_posture_is_UNCHANGED() -> None:
    """No FGA client means authorization is off estate-wide; this route does not invent its own posture."""
    out = await show(instance_for("tok-1"), _request(fga_client=None), None)  # ty: ignore[invalid-argument-type]

    assert out.project == "acme"
