"""The inbox door, driven through a real app.

The app is built bare rather than through `make_service_app` on purpose: this suite is about the
ROUTES and their dependency resolution, and `make_service_app` would drag in settings loading, OTel
and middleware that belong to other gates. What it does keep is the estate's exception handlers, so a
refusal is asserted as the problem+json the browser would actually receive.

The inbox double answers with `notifications.feed`'s own rules, so what these tests exercise is the
DOOR — paging, the cursor, the visibility filter, the fields a response is allowed to carry — over a
store that behaves like the actor rather than like a mock's expectations.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import inbox as inbox_module
from notifications.api import visibility as visibility_module
from notifications.config import get_notifications_settings
from notifications.feed import paginate, unread_count
from notifications.models import InboxDismiss, InboxMark, InboxPointer, InboxQuery, NotificationReason
from notifications.proxies import TypedActorProxy
from service_kit.exceptions import register_handlers


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _pointer(index: int, *, obj: str = "silver$pages", seen: bool = False, dismissed: bool = False) -> InboxPointer:
    return InboxPointer(
        notification_id=f"run-{index:03d}@FAIL",
        reason=NotificationReason.AUTHOR,
        object_id=obj,
        source_run_id=f"ingest-{index}",
        occurred_at=NOW - timedelta(minutes=index),
        seen=seen,
        dismissed=dismissed,
    )


class _Inbox:
    """One subject's inbox, answering with the actor's own pure rules."""

    def __init__(self, rows: list[InboxPointer]) -> None:
        self.rows = rows

    async def page(self, payload: dict[str, Any]) -> dict[str, Any]:
        return paginate(self.rows, InboxQuery.model_validate(payload)).model_dump(mode="json")

    async def unread(self) -> dict[str, Any]:
        return {"unread": unread_count(self.rows), "rows": len(self.rows)}

    async def mark_seen(self, payload: dict[str, Any]) -> dict[str, Any]:
        wanted = set(InboxMark.model_validate(payload).notification_ids)
        updated = [row.marked_seen() if row.notification_id in wanted and not row.seen else row for row in self.rows]
        changed = sum(1 for before, after in zip(self.rows, updated, strict=True) if before is not after)
        self.rows = updated
        return {"updated": changed, "unread": unread_count(self.rows), "rows": len(self.rows)}

    async def dismiss(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = InboxDismiss.model_validate(payload).notification_id
        updated = [row.marked_dismissed() if row.notification_id == target and not row.dismissed else row for row in self.rows]
        changed = sum(1 for before, after in zip(self.rows, updated, strict=True) if before is not after)
        self.rows = updated
        return {"dismissed": changed, "unread": unread_count(self.rows), "rows": len(self.rows)}


@pytest.fixture
def inbox() -> _Inbox:
    return _Inbox([])


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, inbox: _Inbox) -> Iterator[TestClient]:
    app = FastAPI()
    register_handlers(app)
    app.include_router(inbox_module.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.actors_registered = True
    # Auth off, so the subject is `anon` and the visibility filter is pass-through — the dev shape.
    app.state.fga = None
    monkeypatch.setattr(inbox_module, "inbox_for", lambda _subject: cast(TypedActorProxy, inbox))
    with TestClient(app) as test_client:
        yield test_client


def test_an_inbox_nobody_has_written_to_is_empty_rather_than_missing(client: TestClient) -> None:
    response = client.get("/notifications/inbox")
    assert response.status_code == 200
    assert response.json() == {"notifications": [], "next_cursor": None, "unread": 0}


def test_a_page_is_newest_first(client: TestClient, inbox: _Inbox) -> None:
    inbox.rows = [_pointer(3), _pointer(1), _pointer(2)]
    ids = [row["notification_id"] for row in client.get("/notifications/inbox").json()["notifications"]]
    assert ids == ["run-001@FAIL", "run-002@FAIL", "run-003@FAIL"]


def test_exactly_limit_rows_hands_back_no_cursor(client: TestClient, inbox: _Inbox) -> None:
    """The boundary a feed gets wrong: a full page is not evidence of another one."""
    inbox.rows = [_pointer(n) for n in range(1, 4)]
    body = client.get("/notifications/inbox", params={"limit": 3}).json()
    assert len(body["notifications"]) == 3
    assert body["next_cursor"] is None


def test_one_row_over_the_limit_hands_back_a_cursor_that_resumes_exactly_after_it(client: TestClient, inbox: _Inbox) -> None:
    inbox.rows = [_pointer(n) for n in range(1, 5)]

    first = client.get("/notifications/inbox", params={"limit": 3}).json()
    assert [row["notification_id"] for row in first["notifications"]] == ["run-001@FAIL", "run-002@FAIL", "run-003@FAIL"]
    assert first["next_cursor"] is not None

    second = client.get("/notifications/inbox", params={"limit": 3, "cursor": first["next_cursor"]}).json()
    assert [row["notification_id"] for row in second["notifications"]] == ["run-004@FAIL"]
    assert second["next_cursor"] is None


def test_the_badge_counts_the_inbox_and_not_the_page(client: TestClient, inbox: _Inbox) -> None:
    """A badge derived from a page would shrink as the reader scrolls."""
    inbox.rows = [_pointer(n) for n in range(1, 6)]
    body = client.get("/notifications/inbox", params={"limit": 2}).json()
    assert len(body["notifications"]) == 2
    assert body["unread"] == 5


def test_the_unread_filter_hides_read_rows(client: TestClient, inbox: _Inbox) -> None:
    inbox.rows = [_pointer(1), _pointer(2, seen=True), _pointer(3, dismissed=True)]
    body = client.get("/notifications/inbox", params={"state": "unread"}).json()
    assert [row["notification_id"] for row in body["notifications"]] == ["run-001@FAIL"]


def test_dismissed_rows_are_absent_under_both_filters(client: TestClient, inbox: _Inbox) -> None:
    inbox.rows = [_pointer(1), _pointer(2, dismissed=True)]
    body = client.get("/notifications/inbox", params={"state": "all"}).json()
    assert [row["notification_id"] for row in body["notifications"]] == ["run-001@FAIL"]


def test_an_unknown_filter_is_refused(client: TestClient) -> None:
    """A `StrEnum` on the query param, so `state=everything` is a 422 rather than a silent `all`."""
    assert client.get("/notifications/inbox", params={"state": "everything"}).status_code == 422


def test_the_page_size_is_capped_by_the_server(client: TestClient) -> None:
    assert client.get("/notifications/inbox", params={"limit": 101}).status_code == 422
    assert client.get("/notifications/inbox", params={"limit": 0}).status_code == 422


def test_the_default_page_size_comes_from_settings(client: TestClient, inbox: _Inbox) -> None:
    """A number a deployment can measure and change, not a literal buried in a route."""
    inbox.rows = [_pointer(n) for n in range(1, 40)]
    body = client.get("/notifications/inbox").json()
    assert len(body["notifications"]) == get_notifications_settings().inbox_page_limit


def test_a_cursor_this_service_did_not_mint_is_refused_as_problem_json(client: TestClient) -> None:
    """Never a silent restart from the top — a client that mistyped a cursor would page forever."""
    response = client.get("/notifications/inbox", params={"cursor": "not-a-cursor"})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")


def test_a_row_whose_object_the_reader_may_no_longer_see_degrades_out_of_the_page(
    client: TestClient,
    inbox: _Inbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pointers are stored and grants are not: a subject whose grant was revoked after delivery still
    holds the row, and the panel resolving it must not disclose an object they can no longer see."""
    from notifications.api import visibility as visibility_module

    async def _only_silver(client_: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        return {obj: obj == "table:silver$pages" for obj in objects}

    monkeypatch.setattr(visibility_module.fga, "batch_check", _only_silver)
    client.app.state.fga = object()
    client.app.state.notifications_settings = get_notifications_settings().model_copy(update={"fga_enabled": True})
    inbox.rows = [_pointer(1), _pointer(2, obj="gold$revoked")]

    body = client.get("/notifications/inbox").json()

    assert [row["notification_id"] for row in body["notifications"]] == ["run-001@FAIL"]


def test_a_page_emptied_by_the_filter_still_hands_back_a_cursor(client: TestClient, inbox: _Inbox, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cursor is the window FLOOR, not the last visible row. A cursor over the visible rows would
    be null on a fully-hidden page and the reader would stop at a wall with more of their own below."""

    async def _nothing(client_: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        return dict.fromkeys(objects, False)

    monkeypatch.setattr(visibility_module.fga, "batch_check", _nothing)
    client.app.state.fga = object()
    client.app.state.notifications_settings = get_notifications_settings().model_copy(update={"fga_enabled": True})
    inbox.rows = [_pointer(n) for n in range(1, 5)]

    body = client.get("/notifications/inbox", params={"limit": 2}).json()

    assert body["notifications"] == []
    assert body["next_cursor"] is not None


def test_the_badge_route_answers_without_the_rows(client: TestClient, inbox: _Inbox) -> None:
    """Its own route because the badge is polled and the feed is not — answering it through the feed
    would load every pointer record to return one integer."""
    inbox.rows = [_pointer(1), _pointer(2, seen=True)]
    assert client.get("/notifications/inbox/unread").json() == {"unread": 1}


def test_marking_rows_read_reports_only_what_changed(client: TestClient, inbox: _Inbox) -> None:
    inbox.rows = [_pointer(1), _pointer(2, seen=True)]
    body = client.post("/notifications/inbox/seen", json={"notification_ids": ["run-001@FAIL", "run-002@FAIL"]}).json()
    assert body == {"updated": 1, "unread": 0}


def test_marking_nothing_is_a_no_op_rather_than_an_error(client: TestClient, inbox: _Inbox) -> None:
    """The panel legitimately renders nothing, and a call that refused the empty case would make
    every caller branch on it."""
    inbox.rows = [_pointer(1)]
    assert client.post("/notifications/inbox/seen", json={"notification_ids": []}).json() == {"updated": 0, "unread": 1}


def test_a_response_cannot_carry_a_field_it_does_not_declare(client: TestClient, inbox: _Inbox) -> None:
    """The actor answers with a row COUNT beside the badge — how many records this subject's state
    partition holds. That is a storage fact, and the only reason it never reaches a browser is that no
    response model declares it."""
    inbox.rows = [_pointer(1)]
    for response in (
        client.post("/notifications/inbox/seen", json={"notification_ids": []}),
        client.post("/notifications/inbox/dismiss", json={"notification_id": "run-001@FAIL"}),
        client.get("/notifications/inbox/unread"),
    ):
        assert "rows" not in response.json()


def test_dismissing_one_state_leaves_the_other_alone(client: TestClient, inbox: _Inbox) -> None:
    """The whole reason the id scheme carries the state: a backend keyed on the run alone would
    silence a FAILED run somebody had dismissed while it was still running."""
    started = _pointer(1)
    failed = started.model_copy(update={"notification_id": "run-001@COMPLETE"})
    inbox.rows = [started, failed]

    assert client.post("/notifications/inbox/dismiss", json={"notification_id": "run-001@FAIL"}).json() == {"dismissed": 1, "unread": 1}

    remaining = client.get("/notifications/inbox").json()["notifications"]
    assert [row["notification_id"] for row in remaining] == ["run-001@COMPLETE"]


def test_dismissing_a_row_that_is_already_gone_changes_nothing(client: TestClient, inbox: _Inbox) -> None:
    inbox.rows = [_pointer(1)]
    assert client.post("/notifications/inbox/dismiss", json={"notification_id": "run-999@FAIL"}).json() == {"dismissed": 0, "unread": 1}


def test_a_malformed_body_is_refused(client: TestClient) -> None:
    assert client.post("/notifications/inbox/dismiss", json={}).status_code == 422
    assert client.post("/notifications/inbox/dismiss", json={"notification_id": ""}).status_code == 422


def test_no_route_lets_a_caller_name_somebody_elses_inbox(client: TestClient) -> None:
    """Identity is DERIVED, never accepted: the actor id is `encode_subject(token.sub)`, so a door
    that accepted a subject anywhere would be a door onto everybody's inbox. The assertion is over
    the whole published surface rather than one route, because "we did not add one" is exactly the
    kind of claim that stops being true later."""
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {
        "/notifications/inbox",
        "/notifications/inbox/unread",
        "/notifications/inbox/seen",
        "/notifications/inbox/dismiss",
    }
    assert not any("{" in path for path in schema["paths"]), "no path parameter — an inbox is never addressed by name"
    declared = {param["name"] for path in schema["paths"].values() for operation in path.values() for param in operation.get("parameters", [])}
    assert declared == {"state", "limit", "cursor"}
    bodies = {field for name, model in schema["components"]["schemas"].items() if name.startswith("Inbox") for field in model.get("properties", {})}
    assert "subject" not in bodies


def test_every_inbox_route_refuses_when_the_actor_plane_is_unregistered(client: TestClient) -> None:
    """A registration failure is logged non-fatal so the health surface survives it — which is only
    survivable because something reads the flag. This is the something."""
    client.app.state.actors_registered = False
    assert client.get("/notifications/inbox").status_code == 503
    assert client.get("/notifications/inbox/unread").status_code == 503
    assert client.post("/notifications/inbox/seen", json={"notification_ids": []}).status_code == 503
    assert client.post("/notifications/inbox/dismiss", json={"notification_id": "run-001@FAIL"}).status_code == 503


def test_a_control_row_survives_the_render_filter_that_hides_lineage_rows(
    client: TestClient,
    inbox: _Inbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE UNCLEARABLE BADGE, and the two gates that disagreed to produce it.

    The control lane delivers WITHOUT a visibility check on purpose — being NAMED is the targeting,
    and after a `grant_revoked` the subject can no longer see the object at all, so a check would drop
    the one event they most need. Then `get_inbox` re-imposed exactly that check at RENDER, over every
    row alike. The result, measured live: a subject with two `task_assigned` rows naming
    `annotation_task:…` objects they hold no grant on saw `unread: 16` beside 14 rows, with the two
    rows in NEITHER page — a badge that cannot be cleared by reading, because what it counts is never
    shown.

    Exempting them discloses nothing further. A rendered row carries `object_id` and no other fact
    about the object (`source_run_id`/`event_seq` are `None` for a control event), and that id is the
    one the subject was already NAMED against — verbatim the argument `control_events` already makes
    for skipping the check at delivery. The lineage rows keep the filter, which is what the sibling
    test above pins: those name a governed DATASET, and a revoked grant must still hide them.
    """
    from notifications.api import visibility as visibility_module

    async def _nothing(client_: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        return dict.fromkeys(objects, False)

    monkeypatch.setattr(visibility_module.fga, "batch_check", _nothing)
    client.app.state.fga = object()
    client.app.state.notifications_settings = get_notifications_settings().model_copy(update={"fga_enabled": True})
    control = InboxPointer(
        notification_id="evt-9@TASK_ASSIGNED",
        reason=NotificationReason.TASK_ASSIGNED,
        object_id="annotation_task:t-9",
        occurred_at=NOW,
    )
    inbox.rows = [_pointer(1), control]

    body = client.get("/notifications/inbox").json()

    ids = [row["notification_id"] for row in body["notifications"]]
    assert ids == ["evt-9@TASK_ASSIGNED"], "the control row must survive; the lineage row must not"
    assert body["unread"] == 2, "the badge still counts the whole inbox — it is not derived from the page"
