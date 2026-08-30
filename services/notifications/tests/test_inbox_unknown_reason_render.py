"""The control lane's render exemption must survive a reason this build cannot NAME.

`_CONTROL_REASONS` is derived from `NAMED_ACTIONS`, and `get_inbox` exempts exactly those rows from
`can_get_metadata` because the control lane delivered them WITHOUT a visibility check — being named
is the targeting, and after a `grant_revoked` the subject cannot see the object at all, so a check
would drop the one event they most need.

`InboxPointer._tolerate_a_newer_vocabulary` rewrites any unrecognised reason to `UNKNOWN`, on purpose,
so a rolled-back build can still read rows a newer one wrote. `UNKNOWN` is in no action set — so the
tolerance path put those rows straight back into the gate the exemption had removed them from. The
sequence is the one `models.py` records as having actually happened on 2026-08-16: a build adds a
named action, rows carrying it land in durable actor state, the deployment rolls back, and the older
build degrades them.

The cost is an UNCLEARABLE badge. The row is dropped from the feed, while `page.unread` (counted over
ALL pointers) and `GET /inbox/unread` (answered from the actor's own partition, which never sees this
filter) still count it. `mark_seen` only names ids the panel rendered, so reading cannot clear it —
for the 30-day default TTL.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import inbox as inbox_module
from notifications.api.security import get_visibility
from notifications.api.visibility import Visibility
from notifications.config import get_notifications_settings
from notifications.models import NotificationReason
from notifications.proxies import TypedActorProxy
from service_kit.exceptions import register_handlers


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

#: A canonical id, carrying its type — what the CONTROL lane stamps.
CONTROL_OBJECT = "annotation_task:proj-1/task-7"
#: A bare dataset name — what a LINEAGE run stamps (`notifiable()` takes `outputs[0]`).
LINEAGE_OBJECT = "silver$pages"


def _row(notification_id: str, reason: str, object_id: str) -> dict[str, Any]:
    return {
        "notification_id": notification_id,
        "reason": reason,
        "object_id": object_id,
        "source_run_id": None,
        "event_seq": None,
        "occurred_at": NOW.isoformat(),
        "seen": False,
        "dismissed": False,
    }


class _Inbox:
    """Three rows: a row whose reason this build cannot name, a known control row, a lineage row."""

    async def page(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "pointers": [
                # `task_review_ready` stands in for an action a NEWER build added. This build has
                # never heard of it, so the model degrades it to `unknown`.
                _row("ctl-new", "task_review_ready", CONTROL_OBJECT),
                _row("ctl-known", NotificationReason.TASK_ASSIGNED.value, "annotation_task:proj-1/task-8"),
                _row("lin-1", NotificationReason.AUTHOR.value, LINEAGE_OBJECT),
            ],
            "has_more": False,
            "unread": 3,
        }

    async def unread(self) -> dict[str, Any]:
        return {"unread": 3, "rows": 3}


class _DenyingFGA:
    """Authorization is ON and the subject holds no `can_get_metadata` on anything.

    That is not a contrived posture — it is precisely the state the control lane exists for: a
    `grant_revoked` or a task dropped out from under someone leaves them named on a row whose object
    they can no longer see.
    """


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def _batch_check(_client: Any, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        return dict.fromkeys(objects, False)

    monkeypatch.setattr("notifications.api.visibility.fga.batch_check", _batch_check)
    app = FastAPI()
    register_handlers(app)
    app.include_router(inbox_module.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.actors_registered = True
    app.state.fga = _DenyingFGA()
    # Authorization ON, injected rather than switched on through settings: `RASK_FGA_ENABLED`
    # legitimately refuses to be set without `RASK_OIDC_ENABLED` ("authz needs a verified subject"),
    # and dragging a bearer through this fixture would test the auth door, not the render gate.
    # Without `enabled=True` `Visibility._filter` short-circuits permissive and the two "must stay
    # governed" cases below would pass while asserting nothing.
    app.dependency_overrides[get_visibility] = lambda: Visibility(client=cast("Any", _DenyingFGA()), enabled=True)
    monkeypatch.setattr(inbox_module, "inbox_for", lambda _subject: cast(TypedActorProxy, _Inbox()))
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _feed(client: TestClient) -> dict[str, Any]:
    resp = client.get("/notifications/inbox")
    assert resp.status_code == 200, f"the door refused: {resp.status_code} {resp.text}"
    return dict(resp.json())


def _ids(body: dict[str, Any]) -> set[str]:
    return {row["notification_id"] for row in body["notifications"]}


def test_a_control_row_whose_reason_DEGRADED_still_renders(client: TestClient) -> None:
    """The wedge. Before the fix this row was counted and never shown."""
    body = _feed(client)

    assert "ctl-new" in _ids(body), f"a degraded control row was filtered out of the feed it is counted in; got {body}"


def test_the_badge_and_the_rows_AGREE_on_a_control_only_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure stated the way a person meets it: a number the panel cannot clear by reading.

    Scoped to a CONTROL-ONLY page on purpose. A lineage row that fails `can_get_metadata` is filtered
    and still counted too, but that is a different and narrower case — this fix does not touch the
    lineage lane, and asserting agreement on a mixed page would claim it did.
    """

    class _ControlOnly:
        async def page(self, _payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "pointers": [
                    _row("ctl-new", "task_review_ready", CONTROL_OBJECT),
                    _row("ctl-known", NotificationReason.TASK_ASSIGNED.value, "annotation_task:proj-1/task-8"),
                ],
                "has_more": False,
                "unread": 2,
            }

        async def unread(self) -> dict[str, Any]:
            return {"unread": 2, "rows": 2}

    monkeypatch.setattr(inbox_module, "inbox_for", lambda _subject: cast(TypedActorProxy, _ControlOnly()))

    body = _feed(client)

    assert body["unread"] == len(body["notifications"]), f"the badge counts rows the panel cannot render: {body}"


def test_a_KNOWN_control_row_is_unaffected(client: TestClient) -> None:
    body = _feed(client)

    assert "ctl-known" in _ids(body)


def test_a_LINEAGE_row_is_still_governed(client: TestClient) -> None:
    """The exemption must not widen. A bare dataset name is the lineage lane, and a denied subject
    must not see it — otherwise this fix would be a disclosure, not a repair."""
    body = _feed(client)

    assert "lin-1" not in _ids(body), "the render gate stopped applying to the lineage lane"


def test_an_UNKNOWN_reason_on_a_BARE_id_stays_governed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrow half of the discriminator, stated as its own case: `UNKNOWN` alone is not a pass.

    Without this the exemption would be "any reason I cannot name", which a lineage row with a
    future reason would satisfy — smuggling a governed object past the check.
    """

    class _BareUnknown:
        async def page(self, _payload: dict[str, Any]) -> dict[str, Any]:
            return {"pointers": [_row("lin-future", "some_future_lineage_reason", LINEAGE_OBJECT)], "has_more": False, "unread": 1}

        async def unread(self) -> dict[str, Any]:
            return {"unread": 1, "rows": 1}

    monkeypatch.setattr(inbox_module, "inbox_for", lambda _subject: cast(TypedActorProxy, _BareUnknown()))

    body = _feed(client)

    assert _ids(body) == set(), "an unknown reason on a bare dataset name must stay governed"
