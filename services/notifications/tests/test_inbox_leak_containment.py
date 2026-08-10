"""What stops a storage field reaching a browser — driven from a store that offers one.

`test_inbox_door_contract.py` proves each response carries exactly its declared keys, over a fake that
answers exactly what the actor answers. That gates the CURRENT actor and not the mechanism: a fake
which never offers a foreign field cannot show whether the door would pass one on.

Two different containments are at work and they are not interchangeable, which is why both are driven
here from the wrong side:

* **the stored record refuses** — `InboxPointer` inherits `extra="forbid"`, so a row carrying a second
  subject's field cannot even be constructed. That is right for a record crossing the actor boundary:
  an unrecognised field there means the store and this code disagree about what a row IS, and reading
  it optimistically is how one subject's state gets served under another's identity.
* **the wire models absorb** — `UnreadBadge` and friends declare no `extra` policy, so the actor's
  `rows` count is dropped on the way out rather than raising. That is right too: the actor answers
  every mutation with a row count on purpose (the read path needs it to know whether an inbox is due
  for compaction), and a response model that refused it would 500 on the happy path.

The pair is the design: refuse an unknown field where it is a disagreement, drop it where it is simply
not the caller's business.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from notifications.api import inbox as inbox_module
from notifications.api.schemas import UnreadBadge
from notifications.config import get_notifications_settings
from notifications.models import InboxPointer, NotificationReason
from notifications.proxies import TypedActorProxy
from service_kit.exceptions import register_handlers


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

#: A subject other than the caller. Its whole job is to be greppable in a response body.
FOREIGN = "bob-9f2c"


def _row(**extra: object) -> dict[str, Any]:
    """One pointer as the STORE hands it over — a JSON object, not a model, because that is the seam."""
    return {
        "notification_id": "run-001@FAIL",
        "reason": NotificationReason.AUTHOR.value,
        "object_id": "silver$pages",
        "source_run_id": "ingest-1",
        "event_seq": None,
        "occurred_at": NOW.isoformat(),
        "seen": False,
        "dismissed": False,
        **extra,
    }


class _LeakingInbox:
    """An inbox whose stored rows carry a field no model declares.

    Not a hypothetical shape: the actor's own partitions DO carry a `subject` (the second lock that
    makes a mismatched record unreadable rather than absent), so "a row with a subject on it" is one
    serialization mistake away, not a fiction invented for this test.
    """

    async def page(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"pointers": [_row(subject=FOREIGN)], "has_more": False, "unread": 1}

    async def unread(self) -> dict[str, Any]:
        return {"unread": 1, "rows": 9, "subject": FOREIGN}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The door over a leaking store. `raise_server_exceptions=False` so a refusal that arrives as a
    500 is a RESPONSE this suite can read, rather than an exception that ends it — what is under test
    is what reaches the wire, and an exception that propagated would leave that unasserted."""
    app = FastAPI()
    register_handlers(app)
    app.include_router(inbox_module.router)
    app.state.notifications_settings = get_notifications_settings()
    app.state.actors_registered = True
    app.state.fga = None
    monkeypatch.setattr(inbox_module, "inbox_for", lambda _subject: cast(TypedActorProxy, _LeakingInbox()))
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_a_stored_row_cannot_even_be_constructed_with_a_field_it_does_not_declare() -> None:
    """The containment is structural — `extra="forbid"` on the record that crosses the actor boundary
    — rather than a filter at the door. A filter is something a later route can forget to apply."""
    with pytest.raises(ValidationError, match="subject"):
        InboxPointer.model_validate(_row(subject=FOREIGN))


def test_a_store_answering_with_another_subjects_field_cannot_put_it_on_the_wire(client: TestClient) -> None:
    """The load-bearing one: a foreign field in the store must not become a foreign field in a browser.

    Not-a-200 rather than a specific code, because the code is the store contract's business and this
    is the door's. But it is not a free choice either: `models.py` states the rule for this record —
    drift on a stored row reads as UNREADABLE, never as absent — so the answer that would fail this
    assertion is the one to worry about, a 200 carrying a row nobody could fully read.
    """
    response = client.get("/notifications/inbox")

    assert response.status_code != 200, "a row the models cannot read was served as if it were fine"
    assert FOREIGN not in response.text


def test_the_badge_absorbs_the_stores_row_count_instead_of_refusing_it(client: TestClient) -> None:
    """The other containment, and the reason the two must differ.

    The actor answers the badge with `{unread, rows}` by design — the small partition holds both — so
    a wire model that forbade extras would turn the most frequent read in the plane into a 500. It
    declares one field instead, and the count is dropped rather than refused.
    """
    response = client.get("/notifications/inbox/unread")

    assert response.status_code == 200
    assert response.json() == {"unread": 1}


def test_the_wire_model_drops_what_it_does_not_declare_rather_than_carrying_it() -> None:
    """The same claim without the door, so a change to either one cannot hide behind the other."""
    assert UnreadBadge.model_validate({"unread": 1, "rows": 9, "subject": FOREIGN}).model_dump() == {"unread": 1}
