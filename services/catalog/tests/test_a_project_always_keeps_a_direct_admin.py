"""A project always keeps a direct admin: the authorization store arbitrates the revoke door's last-admin guard ([[LH-434]]).

The members door refuses to revoke a project's last direct ``admin`` (409, code 14), since a project with none can be
governed through none of its own doors. The count is a read, so two revokes of two admins each read two and each
passed: measured live on helm rev 16, 10 of 10 trials of two admins revoking each other at once left the project with
no admin. The door now deletes the admin tuple and moves the project's ``admin_revision`` in one OpenFGA Write that the
store refuses whole once another revoke moved the revision, and the loser reads again and judges the admins it finds.

Every decision is the real model's, on a store of this test's own. The estate admin's ``owner`` tuple on the estate is
seeded as the chart's bootstrap hook seeds it; everything else goes through the doors: root creates ``acme`` and grants
the rungs, and the ``acme`` fixture says which two direct admins it leaves.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from service_kit.governed import fga


#: The Lance Namespace spec's error codes a stock client reads (`spec.yaml`).
CONCURRENT_MODIFICATION, PERMISSION_DENIED = 14, 15
ALICE, BOB, GINA = ("user:dex~alice", "admin"), ("user:dex~bob", "admin"), ("user:dex~gina", "member")
MEMBERS = "/management/v1/projects/acme/members"


def _revoke(catalog: Any, caller: str, user: str, relation: str) -> httpx.Response:  # noqa: ANN401 — the conftest's GovernedCatalog
    return catalog.client.request("DELETE", MEMBERS, json={"user": user, "relation": relation}, headers=catalog.bearer(caller))


def _ok(response: httpx.Response) -> httpx.Response:
    assert response.status_code == 200, response.text
    return response


def _members(catalog: Any, caller: str) -> list[tuple[str, str]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    members = _ok(catalog.client.get(MEMBERS, headers=catalog.bearer(caller))).json()["members"]
    return sorted((m["user"], m["relation"]) for m in members)


@pytest.fixture
def acme(request: pytest.FixtureRequest, governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """``acme`` with gina a member and two direct admins: alice and bob once alice has revoked root (``settled``, the
    default, an admin revision already written), or root and alice with no revoke yet (``fresh``, no revision)."""
    catalog = governed_catalog
    fresh = getattr(request, "param", "settled") == "fresh"
    catalog.store.write((catalog.person("root"), "owner", "estate:rask"))
    _ok(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=catalog.bearer("root")))
    for user, relation in (("dex~alice", "admin"), ("dex~gina", "member"), *([] if fresh else [("dex~bob", "admin")])):
        _ok(catalog.client.put(MEMBERS, json={"user": user, "relation": relation}, headers=catalog.bearer("root")))
    if not fresh:
        _ok(_revoke(catalog, "alice", "dex~root", "admin"))
    return catalog


@pytest.mark.parametrize(
    ("acme", "pair"),
    [
        pytest.param("settled", ("alice", "bob"), id="after-an-earlier-revoke"),
        pytest.param("fresh", ("root", "alice"), id="the-projects-first-revoke"),
    ],
    indirect=["acme"],
)
def test_two_admins_revoking_each_other_at_once_leave_the_project_one_admin(acme: Any, pair: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN401
    # Both revokes read the project's tuples before either writes: the overlap the live trials hit.
    read = fga.read_object_tuples
    meeting = asyncio.Barrier(2)
    first_reads: list[str] = []

    async def read_together(client: Any, obj: str, **kwargs: Any) -> Any:  # noqa: ANN401 — fga.read_object_tuples' signature
        tuples = await read(client, obj, **kwargs)
        if len(first_reads) < 2:
            first_reads.append(obj)
            await asyncio.wait_for(meeting.wait(), timeout=10)
        return tuples

    monkeypatch.setattr(fga, "read_object_tuples", read_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda revoke: _revoke(acme, revoke[0], f"dex~{revoke[1]}", "admin"), [pair, pair[::-1]]))
    monkeypatch.setattr(fga, "read_object_tuples", read)

    survivor = pair[0] if answers[0].status_code == 200 else pair[1]
    assert sorted((a.status_code, a.json().get("code")) for a in answers) == [(200, None), (409, CONCURRENT_MODIFICATION)], [a.text for a in answers]
    assert _members(acme, survivor) == sorted([(f"user:dex~{survivor}", "admin"), GINA])


@pytest.mark.parametrize(
    ("before", "revoked", "status", "left"),
    [
        pytest.param([], ("dex~bob", "admin"), 200, [ALICE, GINA], id="one-of-two-admins-is-revoked"),
        pytest.param([("dex~bob", "admin")], ("dex~alice", "admin"), 409, [ALICE, GINA], id="the-last-admin-is-refused"),
        pytest.param([], ("dex~gina", "member"), 200, [ALICE, BOB], id="a-member-is-revoked-as-before"),
        pytest.param([], ("dex~nobody", "admin"), 200, [ALICE, BOB, GINA], id="revoking-what-nobody-holds-changes-nothing"),
    ],
)
def test_a_revoke_one_at_a_time_answers_on_the_admins_it_finds(
    acme: Any, before: list[tuple[str, str]], revoked: tuple[str, str], status: int, left: list[tuple[str, str]]
) -> None:  # noqa: ANN401
    for user, relation in before:
        _ok(_revoke(acme, "alice", user, relation))

    answered = _revoke(acme, "alice", *revoked)

    assert answered.status_code == status, answered.text
    if status == 409:
        assert (answered.json()["code"], "only remaining admin" in answered.json()["detail"]) == (CONCURRENT_MODIFICATION, True)
    assert _members(acme, "alice") == left


def test_a_member_cannot_grant_admin(acme: Any) -> None:  # noqa: ANN401
    # Granting a rung asks that rung's own `can_grant_*`: gina holds `member`, which grants no `can_grant_admin`.
    answered = acme.client.put(MEMBERS, json={"user": "dex~gina", "relation": "admin"}, headers=acme.bearer("gina"))

    assert (answered.status_code, answered.json()["code"]) == (403, PERMISSION_DENIED), answered.text
    assert _members(acme, "alice") == [ALICE, BOB, GINA]
