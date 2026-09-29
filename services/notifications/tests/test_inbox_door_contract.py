"""The inbox door's HTTP contract, at the edges: the cursor, the page bounds, and the exact bytes out.

`test_inbox_routes.py` drives the door's ordinary shapes. This suite drives the ones a paging API is
usually wrong about, and each is a claim the design makes explicitly:

* **A cursor is a POSITION, never an identity.** The actor id is derived from the verified token and
  from nothing else, so a cursor lifted from another subject's response must be inert — it may move
  the caller within their OWN feed and can never reach a row that is not theirs. Asserted three ways:
  on the bytes a cursor carries, on a forged one that tries to add a subject, and end-to-end.
* **A cursor this service did not mint is refused WITH A REASON.** The failure this prevents is silent:
  a door that swallowed the error would answer 200 with page one, which reads to a paging client as an
  infinite feed. The old form is reproduced below and shown to do exactly that — "these two differ" is
  true of almost any two inputs and would gate nothing.
* **The tiebreaker is load-bearing.** Two runs can carry the same terminal instant. The old form — a
  cursor over `occurred_at` alone — is reproduced and shown to lose every row that shares an instant.
* **A response carries exactly the fields its model declares**, which is the return type doing its
  second job as the serialization filter.

Every subject here is injected through `dependency_overrides` on the auth dep rather than patched onto
a client: the door's own resolution is what is under test, and a patch would route around it.
"""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from notifications.api import inbox as inbox_module
from notifications.api import security as security_module
from notifications.api.cursor import encode_cursor
from notifications.config import get_notifications_settings
from notifications.feed import paginate, unread_count
from notifications.models import INBOX_PAGE_LIMIT_MAX, InboxDismiss, InboxMark, InboxPointer, InboxQuery, NotificationReason
from notifications.proxies import TypedActorProxy
from service_kit.exceptions import register_handlers
from service_kit.lakehouse.ns_errors import install_problem_handlers


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
CALLER = "alice"
STRANGER = "bob"


def _pointer(notification_id: str, *, minutes_ago: int, obj: str = "silver$pages", seen: bool = False, dismissed: bool = False) -> InboxPointer:
    return InboxPointer(
        notification_id=notification_id,
        reason=NotificationReason.AUTHOR,
        object_id=obj,
        source_run_id=f"ingest-{notification_id}",
        event_seq=None,
        occurred_at=NOW - timedelta(minutes=minutes_ago),
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


class _Plane:
    """Every subject's inbox, and a record of which ones were ever opened.

    The record is the point: "the caller's rows came back" is satisfied by a door that opened both
    inboxes and filtered afterwards, which would be a door that reads a stranger's state.
    """

    def __init__(self) -> None:
        self.boxes: dict[str, _Inbox] = {}
        self.opened: list[str] = []

    def inbox(self, subject: str) -> _Inbox:
        return self.boxes.setdefault(subject, _Inbox([]))

    def open(self, subject: str) -> TypedActorProxy:
        self.opened.append(subject)
        return cast(TypedActorProxy, self.inbox(subject))


@pytest.fixture
def plane() -> _Plane:
    return _Plane()


def _app(plane: _Plane, monkeypatch: pytest.MonkeyPatch, *, resolve_subject: bool) -> FastAPI:
    app = FastAPI()
    # BOTH installers, because `make_service_app` — which builds the real app — installs both, and the
    # governed door raises across both taxonomies: a missing bearer is a `lance_namespace`
    # `UnauthenticatedError` (so its 401 carries the spec `code`), an unwired verifier a fleet
    # `ServiceUnavailableError` (so its 503 keeps the message naming the knob). With only the fleet
    # half this fixture answered a shape the deployed service never produces.
    register_handlers(app)
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(inbox_module.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.actors_registered = True
    app.state.fga = None
    if resolve_subject:
        # The verified subject, injected through the door's OWN dependency. Overriding it (rather than
        # patching the route) is what keeps `CurrentSubject` in the request path — a route that stopped
        # asking for one would stop being handed one.
        app.dependency_overrides[security_module._deps.current_subject] = lambda: CALLER
    monkeypatch.setattr(inbox_module, "inbox_for", plane.open)
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, plane: _Plane) -> Iterator[TestClient]:
    with TestClient(_app(plane, monkeypatch, resolve_subject=True)) as test_client:
        yield test_client


@pytest.fixture
def unauthenticated(monkeypatch: pytest.MonkeyPatch, plane: _Plane) -> Iterator[TestClient]:
    """The same door with NOTHING standing in for the subject — the authn chain resolves for real.

    Its own fixture because the override the other tests use short-circuits exactly the dependency
    these assertions are about.
    """
    with TestClient(_app(plane, monkeypatch, resolve_subject=False)) as test_client:
        yield test_client


# ── a cursor is a position, never an identity ───────────────────────────────────────────────────────


def test_a_cursor_lifted_from_another_subjects_feed_reads_only_the_callers_own_rows(client: TestClient, plane: _Plane) -> None:
    """The replay, end to end: the stranger's cursor moves the caller within the caller's own feed.

    It is chosen to be a cursor that MATTERS — its instant sits inside the caller's range, so honouring
    it as a position changes which of the caller's rows come back. What it must never do is name a
    store. `plane.opened` is asserted because "only the caller's rows came back" would also be true of
    a door that read both inboxes and filtered afterwards.
    """
    plane.inbox(CALLER).rows = [_pointer(f"alice-{n:03d}@FAIL", minutes_ago=n) for n in (1, 3, 5)]
    plane.inbox(STRANGER).rows = [_pointer(f"bob-{n:03d}@FAIL", minutes_ago=n) for n in (2, 4)]
    stranger_cursor = encode_cursor(plane.inbox(STRANGER).rows[0])

    body = client.get("/notifications/inbox", params={"cursor": stranger_cursor}).json()

    returned = [row["notification_id"] for row in body["notifications"]]
    assert returned == ["alice-003@FAIL", "alice-005@FAIL"], "the cursor moved the caller inside their own feed"
    assert not any(row.startswith("bob-") for row in returned)
    assert plane.opened == [CALLER], f"a stranger's inbox was opened: {plane.opened}"


# ── a cursor this service did not mint is refused, with a reason ────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("outside the alphabet", "!!!!"),
        ("valid base64, wrong shape", "eyJhIjogMX0"),
        ("valid base64, empty object", "e30"),
    ],
)
def test_a_cursor_this_service_did_not_mint_is_refused_with_a_reason_and_serves_no_row(client: TestClient, plane: _Plane, label: str, raw: str) -> None:
    """4xx naming itself — never a 500, and never a page.

    The body is asserted, not only the status: a refusal a client cannot act on is a refusal that gets
    retried forever. `notifications` must be absent from the body entirely, because the failure being
    prevented is a full scan served as if it were the requested page.
    """
    plane.inbox(CALLER).rows = [_pointer(f"alice-{n:03d}@FAIL", minutes_ago=n) for n in range(1, 6)]

    response = client.get("/notifications/inbox", params={"cursor": raw})

    assert response.status_code == 400, label
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert "cursor" in body["detail"], f"the refusal must name what was wrong; got {body!r}"
    assert body["status"] == 400
    assert "notifications" not in body, "a refused cursor must not come back carrying rows"


# ── the tiebreaker, and what its absence costs ──────────────────────────────────────────────────────


def test_paging_rows_that_share_an_instant_repeats_nothing_and_skips_nothing(client: TestClient, plane: _Plane) -> None:
    """Five rows, one instant, pages of two: the walk must visit each row exactly once.

    A batch of runs finishing together is not exotic — the cascade fans out and their terminal events
    carry the same stamp. This is the case where an `occurred_at`-only cursor loses rows.
    """
    plane.inbox(CALLER).rows = [_pointer(f"alice-{n:03d}@FAIL", minutes_ago=7) for n in range(1, 6)]

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = client.get("/notifications/inbox", params=params).json()
        seen.extend(row["notification_id"] for row in body["notifications"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "the walk did not terminate"
    assert seen == sorted(seen, reverse=True), "the order is not total — equal instants must fall back to the id"
    assert len(seen) == len(set(seen)) == 5, f"the walk repeated or skipped a row: {seen}"


# ── the page bound, named at both edges ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("limit", "status"),
    [(1, 200), (INBOX_PAGE_LIMIT_MAX - 1, 200), (INBOX_PAGE_LIMIT_MAX, 200), (INBOX_PAGE_LIMIT_MAX + 1, 422)],
)
def test_the_page_size_bound_is_inclusive_at_the_cap(client: TestClient, limit: int, status: int) -> None:
    """Both sides of the cap by name. The interesting one is `INBOX_PAGE_LIMIT_MAX` itself: an `lt=`
    written where an `le=` was meant refuses the documented maximum and passes every other case."""
    assert client.get("/notifications/inbox", params={"limit": limit}).status_code == status


# ── what a response is allowed to contain ───────────────────────────────────────────────────────────


def test_a_populated_feed_carries_exactly_the_fields_its_models_declare(client: TestClient, plane: _Plane) -> None:
    """Driven with every optional field populated, because a leak hides in the fields a sparse fixture
    leaves null.

    The row IS the claim-check record and is returned whole — an id, a reason, one object name, a run
    link, a sequence and two booleans. What must never appear is anything from the storage record: the
    actor's `subject` second-lock and its `rows` count are facts about the STORE, and the only thing
    keeping them off the wire is that no response model declares them.
    """
    populated = _pointer("alice-001@FAIL", minutes_ago=1).model_copy(update={"event_seq": 41, "seen": True})
    plane.inbox(CALLER).rows = [populated, _pointer("alice-002@FAIL", minutes_ago=2)]

    body = client.get("/notifications/inbox").json()

    assert set(body) == {"notifications", "next_cursor", "unread"}
    assert set(body["notifications"][0]) == {"notification_id", "reason", "object_id", "source_run_id", "event_seq", "occurred_at", "seen", "dismissed"}
    assert "subject" not in json.dumps(body)


# ── every other refusal, likewise ───────────────────────────────────────────────────────────────────


class _Verifier:
    """Stands in for `OIDCVerifier`. It is never reached — the refusals below happen before a token is
    parsed — but its PRESENCE is what separates "no verifier" from "no bearer", which are the two
    outcomes that must not be confused."""

    def verify(self, credentials: str) -> object:
        raise AssertionError(f"no test here presents a bearer; got {credentials!r}")


def test_authentication_enabled_but_unwired_refuses_instead_of_serving_an_anonymous_inbox(unauthenticated: TestClient, plane: _Plane) -> None:
    """The middle outcome of the three-outcome rule, on the authn side.

    A verifier that failed to build is a BROKEN authentication layer, and the permissive reading of it
    would serve `anon`'s inbox to whoever asked — on a plane whose entire premise is that a badge counts
    your own work. It is logged non-fatal at boot precisely so this dependency can be the thing that
    refuses.
    """
    unauthenticated.app.state.notifications_settings = get_notifications_settings().model_copy(update={"oidc_enabled": True})

    response = unauthenticated.get("/notifications/inbox")

    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"].lower()
    assert plane.opened == [], "the door reached an inbox before deciding who was asking"


def test_a_request_with_no_bearer_is_refused_by_name_rather_than_read_as_anonymous(unauthenticated: TestClient, plane: _Plane) -> None:
    """With authentication ON and a verifier present, an unauthenticated read is a 401 that says so.

    The alternative is not a 403 or an empty page — it is silently answering as `anon`, which with
    subject-derived routing means every unauthenticated caller shares one inbox.
    """
    unauthenticated.app.state.oidc = _Verifier()
    unauthenticated.app.state.notifications_settings = get_notifications_settings().model_copy(update={"oidc_enabled": True})

    response = unauthenticated.get("/notifications/inbox")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "bearer" in response.json()["detail"].lower()
    assert plane.opened == []


def test_authorization_enabled_but_unwired_refuses_a_page_it_cannot_filter(client: TestClient, plane: _Plane) -> None:
    """The same rule on the authz side, and the one with a subtle edge.

    `Visibility.visible` short-circuits an EMPTY candidate set before the unwired-client refusal, so an
    empty inbox still answers 200 during an OpenFGA outage — deliberately, since there is nothing an
    outage could disclose by answering "you have no notifications". A page WITH rows is the other case:
    the filter cannot run, so the rows must not be served, and serving them unfiltered would publish
    objects the reader may no longer see.
    """
    plane.inbox(CALLER).rows = [_pointer("alice-001@FAIL", minutes_ago=1)]
    client.app.state.fga = None
    client.app.state.notifications_settings = get_notifications_settings().model_copy(update={"fga_enabled": True})

    response = client.get("/notifications/inbox")

    assert response.status_code == 503
    assert "authorization" in response.json()["detail"].lower()
    assert "notifications" not in response.json()


def test_an_empty_inbox_is_not_turned_into_an_outage_by_the_same_setting(client: TestClient) -> None:
    """The other side of that edge, asserted so the refusal above is not read as "503 whenever FGA is
    unwired". A reader with nothing to filter gets their empty inbox, because 503ing it would turn
    "you have no notifications" into an error for every idle user during an OpenFGA blip."""
    client.app.state.fga = None
    client.app.state.notifications_settings = get_notifications_settings().model_copy(update={"fga_enabled": True})

    response = client.get("/notifications/inbox")

    assert response.status_code == 200
    assert response.json()["notifications"] == []
