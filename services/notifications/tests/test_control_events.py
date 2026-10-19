"""v3 targeting — governance events that NAME a subject.

The claim under test is the one that looks like a hole and is not: this lane runs NO visibility
check. Being named IS the targeting, and after a `grant_revoked` the subject can no longer see the
object — so a delivery-time visibility check would drop precisely the event they most need.
"""

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from notifications.api.control_events import as_delivery, ingest_control_event, named_subjects
from service_kit.control_events import CatalogControlEvent


def _event(*, action: str = "grant_added", subject: str | None = "user:dex~alice", object_id: str = "table:acme$gold") -> CatalogControlEvent:
    extra: dict[str, Any] = {"relation": "reader"}
    if subject is not None:
        extra["subject"] = subject
    return CatalogControlEvent(
        event_id="evt-1",
        occurred_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        # `cast` rather than an ignore comment: the parametrized cases deliberately drive actions the
        # Literal allows, and one that it does not is the point of the "names nobody" case.
        action=cast(Any, action),
        object_type=cast(Any, "table"),
        object_id=object_id,
        actor="user:dex~admin",
        extra=extra,
    )


class _Inbox:
    def __init__(self, plane: "_Plane", subject: str) -> None:
        self._plane = plane
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._subject in self._plane.broken:
            raise RuntimeError("the sidecar is unreachable")
        self._plane.boxes.setdefault(self._subject, []).append(payload)
        return {"delivered": True}


class _Plane:
    def __init__(self, broken: set[str] | None = None) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}
        self.broken = broken or set()

    def open(self, subject: str) -> Any:
        return _Inbox(self, subject)


class TestNamedSubject:
    @pytest.mark.parametrize(
        ("stamped", "inbox"),
        [
            pytest.param("user:dex~alice", ("dex~alice",), id="a-person-is-the-principal-their-user-names"),
            pytest.param("user:corp~a%40b", ("corp~a@b",), id="an-encoded-character-addresses-the-inbox-its-token-reaches"),
            pytest.param("user:kubernetes~system%3Aserviceaccount%3Adefault%3Arask-sa-catalog", (), id="a-machine-is-no-inbox"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_grant_names_its_grantee(self, stamped: str, inbox: tuple[str, ...]) -> None:
        """A grant stamps the OpenFGA user it names (`fga.user_object`), and an inbox is addressed by the principal key a
        person's own token proves ([[LH-063]]): the stamped id is decoded, and only a person is told."""
        assert await named_subjects(_event(action="grant_added", subject=stamped)) == inbox

    @pytest.mark.parametrize("action", ["warehouse_bound"])
    @pytest.mark.asyncio
    async def test_an_action_that_names_nobody_targets_nobody(self, action: str) -> None:
        """Every other control action is a catalog mutation with no named party. Delivering those
        would recreate the estate-wide feed this plane exists to replace."""
        assert await named_subjects(_event(action=action)) == ()

    @pytest.mark.asyncio
    async def test_a_grant_with_no_subject_names_nobody(self) -> None:
        """`extra` is an open bag on an envelope this service does not own: a producer that stops
        stamping it makes this lane quiet, never wrong."""
        assert await named_subjects(_event(subject=None)) == ()

    @pytest.mark.parametrize("subject", ["", "user:"])
    @pytest.mark.asyncio
    async def test_an_empty_subject_names_nobody(self, subject: str) -> None:
        assert await named_subjects(_event(subject=subject)) == ()

    @pytest.mark.parametrize("subject", ["user:*", "user:*  "])
    @pytest.mark.asyncio
    async def test_a_wildcard_subject_names_nobody(self, subject: str) -> None:
        """THE MANAGED-ACCESS DEFECT. `POST .../managed-access` writes the FGA WILDCARD principal
        (`_MANAGED_ACCESS_SUBJECT = "user:*"`, `catalog/api/v1/endpoints/access.py:455`) and then emits
        `grant_added`/`grant_revoked` stamping that same value as `extra.subject`. Stripping the type
        prefix left a truthy `"*"`, so every managed-access toggle in the estate wrote a real pointer
        into an inbox actor literally named `*` — an actor no person can ever open, accumulating rows
        about objects nobody was granted.

        Not a disclosure (nothing reads that inbox) but not inert either: it is unbounded junk in the
        actor state store, and it makes the lane's delivered-count lie about how many people were told.

        A wildcard is a statement about EVERYONE, which is precisely what this lane cannot address —
        its entire contract is that being NAMED is the targeting. `user:*` names no one, so it must be
        treated exactly like the absent subject above: quiet, never wrong."""
        assert await named_subjects(_event(subject=subject)) == ()


class TestDeliveryProjection:
    def test_the_id_is_event_scoped_not_run_scoped(self) -> None:
        """A governance event has no run, and reusing `run_id@STATE` would let a grant collide with a
        run that happened to share an id."""
        assert as_delivery(_event()).notification_id == "evt-1@GRANT_ADDED"


class TestIngest:
    @pytest.mark.asyncio
    async def test_a_named_grant_reaches_that_subject_and_nobody_else(self) -> None:
        plane = _Plane()

        assert await ingest_control_event(_event().model_dump(mode="json"), open_inbox=plane.open) == {"status": "SUCCESS"}

        assert list(plane.boxes) == ["dex~alice"]

    @pytest.mark.asyncio
    async def test_a_revoke_is_delivered_even_though_the_subject_can_no_longer_see_the_object(self) -> None:
        """THE claim of this lane. No visibility check runs, deliberately — a revoked subject fails
        every check on the object, and dropping the row would make losing access silent."""
        plane = _Plane()

        await ingest_control_event(_event(action="grant_revoked").model_dump(mode="json"), open_inbox=plane.open)

        assert plane.boxes["dex~alice"][0]["reason"] == "grant_revoked"

    @pytest.mark.asyncio
    async def test_an_event_naming_nobody_is_acked_rather_than_dropped(self) -> None:
        """It arrived intact; this plane simply has no audience for it. DROP is for what cannot parse."""
        plane = _Plane()

        assert await ingest_control_event(_event(action="table_created").model_dump(mode="json"), open_inbox=plane.open) == {"status": "SUCCESS"}
        assert plane.boxes == {}

    @pytest.mark.asyncio
    async def test_an_unparseable_payload_is_dropped_not_retried(self) -> None:
        """Redelivery cannot make a malformed payload parse, and retrying it forever is how a
        subscription stops delivering anything at all."""
        plane = _Plane()

        assert await ingest_control_event({"nonsense": True}, open_inbox=plane.open) == {"status": "DROP"}

    @pytest.mark.asyncio
    async def test_an_unreachable_inbox_retries(self) -> None:
        """Unlike the watcher lookup, redelivery genuinely helps: the actor is momentarily
        unreachable, and the delivery is idempotent on the notification id."""
        plane = _Plane(broken={"dex~alice"})

        assert await ingest_control_event(_event().model_dump(mode="json"), open_inbox=plane.open) == {"status": "RETRY"}


class TestUsersetGrants:
    """A grant to a ROLE or TEAM must reach its members, not a phantom actor named after the group.

    THE LIVE DEFECT. `named_subject` stripped a `user:` prefix and returned whatever was left, so a
    grant to `role:reviewers#assignee` — which `model.fga` permits on nearly every grantable relation
    (`owner`, `writer`, `reader`, `validator`, `manage_grants`) — was delivered to an InboxActor keyed
    `role:reviewers#assignee`. No person can ever open that actor. Every userset grant in the estate
    was silently accumulating unreadable state while telling none of the people it actually affected.

    REFUSING would have been the safe-looking fix and the wrong one: roles are the estate's primary
    grouping mechanism, so refusing means the most common way to grant access notifies nobody — the
    exact coverage hole this lane exists to close. Expansion is the answer, through the same
    `list_users` primitive the access review uses, injected as a callable so the lane stays testable
    with no FGA behind it (the seam `WatcherLookup` already is).
    """

    def _event(self, subject: str) -> CatalogControlEvent:
        return CatalogControlEvent(
            event_id="evt-us-1",
            occurred_at=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
            action=cast(Any, "grant_added"),
            object_type=cast(Any, "table"),
            object_id="table:acme$gold",
            actor="user:dex~admin",
            extra={"relation": "reader", "subject": subject},
        )

    @pytest.mark.asyncio
    async def test_a_role_grant_reaches_every_member(self) -> None:
        plane = _Plane()

        async def expand(userset: str) -> tuple[str, ...]:
            assert userset == "role:reviewers#assignee"
            return ("dex~bob", "dex~carol")

        result = await ingest_control_event(self._event("role:reviewers#assignee").model_dump(mode="json"), open_inbox=plane.open, expand=expand)

        assert result == {"status": "SUCCESS"}
        assert sorted(plane.boxes) == ["dex~bob", "dex~carol"]
        assert "role:reviewers#assignee" not in plane.boxes, "the group itself must never get an inbox"

    @pytest.mark.asyncio
    async def test_a_plain_user_needs_no_expansion(self) -> None:
        """The common case must not grow an FGA round-trip: a bare principal is already an address."""
        plane = _Plane()
        calls: list[str] = []

        async def expand(userset: str) -> tuple[str, ...]:
            calls.append(userset)
            return ()

        await ingest_control_event(self._event("user:dex~alice").model_dump(mode="json"), open_inbox=plane.open, expand=expand)

        assert list(plane.boxes) == ["dex~alice"]
        assert calls == [], "a plain principal must not be expanded"

    @pytest.mark.asyncio
    async def test_an_unexpandable_userset_tells_nobody_rather_than_a_phantom(self) -> None:
        """With no expander wired — a deployment with FGA off — a userset resolves to no audience at
        all. Quiet, never wrong: the alternative is the phantom actor this whole class exists to end."""
        plane = _Plane()

        result = await ingest_control_event(self._event("role:reviewers#assignee").model_dump(mode="json"), open_inbox=plane.open)

        assert result == {"status": "SUCCESS"}
        assert plane.boxes == {}
