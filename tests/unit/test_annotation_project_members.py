"""Membership on an ANNOTATION project is announced to the person it is about.

The gap this closes is the annotator's half of the same asymmetry the catalog's tenant door had: the
FGA tuples were written and nothing was said. Being added to a labeling project arrived in silence,
and being removed from one arrived as a 403 in the middle of a task — the exact failure the control
lane's own docstring names as its reason to exist.

The handlers are driven directly rather than over HTTP: what is under test is which event each door
emits and who it names, and a TestClient would add a routing layer that answers neither question.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from annotator.api.v1.endpoints import members
from openfga_sdk import OpenFgaClient

from service_kit.exceptions import ConflictError


PROJECT = "proj-7"
OBJ = f"annotation_project:{PROJECT}"
MANAGER = "alice"


class _RecordingControl:
    """The control bus's seat — holds what the door actually announced."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _tuple(user: str, relation: str, obj: str = OBJ) -> SimpleNamespace:
    return SimpleNamespace(user=user, relation=relation, object=obj)


class _Store:
    """A recording stand-in for the FGA store: reads answer from `rows`, writes/deletes mutate it."""

    def __init__(self, rows: list[SimpleNamespace] | None = None) -> None:
        self.rows = rows if rows is not None else []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def read(_c: object, _obj: str, **_kw: object) -> list[SimpleNamespace]:
            return list(self.rows)

        async def write(_c: object, tuples: list[Any], **_kw: object) -> None:
            self.rows.extend(_tuple(t.user, t.relation, t.object) for t in tuples)

        async def delete(_c: object, tuples: list[Any], **_kw: object) -> None:
            gone = {(t.user, t.relation, t.object) for t in tuples}
            self.rows = [r for r in self.rows if (r.user, r.relation, r.object) not in gone]

        monkeypatch.setattr(members.fga, "read_object_tuples", read)
        monkeypatch.setattr(members.fga, "write_tuples", write)
        monkeypatch.setattr(members.fga, "delete_tuples", delete)


async def _allow(*, user: str, relation: str, obj: str) -> bool:
    del user, relation, obj  # the gate is not what these tests are about
    return True


def _grant(relation: str, user: str, control: _RecordingControl) -> Any:
    body = members.GrantRequest(user=user, relation=cast(Any, relation))
    return asyncio.run(members.grant_member(PROJECT, body, _allow, MANAGER, cast(OpenFgaClient, object()), cast(Any, control)))


def _revoke(relation: str, user: str, control: _RecordingControl) -> Any:
    body = members.GrantRequest(user=user, relation=cast(Any, relation))
    return asyncio.run(members.revoke_member(PROJECT, body, _allow, MANAGER, cast(OpenFgaClient, object()), cast(Any, control)))


def test_a_grant_names_the_person_who_received_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _Store().install(monkeypatch)
    control = _RecordingControl()

    _grant("annotator", "bob", control)

    assert len(control.events) == 1
    event = control.events[0]
    assert event.action == "grant_added"
    assert event.extra["subject"] == "user:bob", "the audience is the grantee, never the manager who granted"
    assert event.actor == f"user:{MANAGER}"
    assert event.object_id == OBJ


def test_a_revoke_names_the_person_who_lost_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sharper half: after this bob cannot see the project, so no visibility-gated feed could ever
    have told him. Being NAMED is the targeting."""
    _Store([_tuple("user:bob", "annotator"), _tuple("user:alice", "owner")]).install(monkeypatch)
    control = _RecordingControl()

    _revoke("annotator", "bob", control)

    assert [(e.action, e.extra["subject"]) for e in control.events] == [("grant_revoked", "user:bob")]


def test_a_repeated_grant_announces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-granting what someone already holds is a no-op on the store and must be one on the bus:
    a row in their inbox for a change that did not happen is how a bell stops being read."""
    _Store([_tuple("user:bob", "annotator")]).install(monkeypatch)
    control = _RecordingControl()

    _grant("annotator", "bob", control)

    assert control.events == []


def test_the_last_administrative_grant_is_refused_and_announces_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal is not a change. The door already guards the last owner/manager; this pins that the
    announcement follows the MUTATION rather than the request — nothing committed, nothing said."""
    _Store([_tuple("user:alice", "owner")]).install(monkeypatch)
    control = _RecordingControl()

    with pytest.raises(ConflictError):
        _revoke("owner", "alice", control)

    assert control.events == []
