"""A warehouse's rungs are granted, revoked and listed through its own doors, on the grant axis alone ([[LH-555]]).

`POST /management/v1/warehouse/{id}/access/{grant,revoke,list}` is the table and namespace doors' shape: a grant asks
`can_grant_<rung>` on the warehouse, a revoke `can_revoke_grant`, a listing `can_read_assignments`, and a grant that would
raise the caller's own access is refused. A project's admin holds all three through `can_administer from project`; a
member of the project holds none, and nor does a `manage_grants` holder handing themselves a rung they lack. Each case
is judged by the tuples the store holds after the one request.

Against the catalog as deployed with warehouses on, a loopback Dex signing callers in and a real OpenFGA deciding on a
store of the test's own ([[LH-235]]).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from catalog.core.config import get_settings
from catalog.services import warehouses


PROJECT, WAREHOUSE = "acme", "wh-acme"
#: acme's admin, who made it; a member of acme; a holder of `manage_grants` on the warehouse alone; the grantee.
ADMIN, MEMBER, GRANTER, GRANTEE = "alice", "carol", "judy", "erin"
#: A refusal: its status and spec code (`lance_docs/ns_catalog/namespace/operations/errors.md`, PermissionDenied).
REFUSED = (403, 15)
#: What a door answered: a refusal's status and code, a grant's or revoke's body, or who a listing names on `can_create_table`.
type Answered = tuple[int, int] | dict[str, object] | list[str]


@pytest.fixture
def estate(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Any:
    """acme, made through its door by alice, and acme's warehouse, written where its door keeps it, with carol a member
    of acme and judy holding `manage_grants` on the warehouse."""
    monkeypatch.setenv("LANCE_WAREHOUSES_ENABLED", "true")
    catalog = request.getfixturevalue("governed_catalog")
    catalog.store.write((catalog.person(ADMIN), "owner", "estate:rask"))
    made = catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ADMIN))
    assert made.status_code == 200, made.text
    settings = get_settings()
    record = {
        "id": WAREHOUSE,
        "bucket": WAREHOUSE,
        "root_uri": settings.root,
        "project": PROJECT,
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
    }
    warehouses.create_warehouse_record(settings.registry_root, settings.storage_options(), record)
    catalog.store.write(
        (f"project:{PROJECT}", "project", f"warehouse:{WAREHOUSE}"),
        (catalog.person(MEMBER), "member", f"project:{PROJECT}"),
        (catalog.person(GRANTER), "manage_grants", f"warehouse:{WAREHOUSE}"),
    )
    return catalog


def _held(estate: Any, user: str) -> set[str]:
    """The rungs ``user`` holds directly on the warehouse, but the `manage_grants` the fixture gave judy."""
    return {relation for held, relation, obj in estate.store.tuples() if held == estate.person(user) and obj == f"warehouse:{WAREHOUSE}"} - {"manage_grants"}


def _answered(door: str, answer: Any) -> Answered:
    """What ``door`` answered, in the one shape each case states."""
    if answer.status_code != 200:
        return (answer.status_code, answer.json()["code"])
    if door == "list":
        return next(grants["users"] for grants in answer.json()["grants"] if grants["relation"] == "can_create_table")
    return answer.json()


def _granted(relation: str, *, granted: bool) -> dict[str, object]:
    return {"object": f"warehouse:{WAREHOUSE}", "name": None, "user": f"user:dex~{GRANTEE}", "relation": relation, "granted": granted}


@pytest.mark.parametrize(
    ("caller", "door", "grantee", "relation", "holding", "answered", "after"),
    [
        pytest.param(ADMIN, "grant", GRANTEE, "writer", set(), _granted("writer", granted=True), {"writer"}, id="the-project-admin-grants-a-rung"),
        pytest.param(ADMIN, "revoke", GRANTEE, "writer", {"writer"}, _granted("writer", granted=False), set(), id="the-project-admin-revokes-a-rung"),
        pytest.param(ADMIN, "list", GRANTEE, "writer", {"writer"}, [f"dex~{ADMIN}", f"dex~{GRANTEE}"], {"writer"}, id="the-project-admin-lists-who-holds-what"),
        pytest.param(MEMBER, "grant", GRANTEE, "writer", set(), REFUSED, set(), id="a-project-member-is-refused-granting"),
        pytest.param(MEMBER, "revoke", GRANTEE, "writer", {"writer"}, REFUSED, {"writer"}, id="a-project-member-is-refused-revoking"),
        pytest.param(MEMBER, "list", GRANTEE, "writer", {"writer"}, REFUSED, {"writer"}, id="a-project-member-is-refused-the-listing"),
        pytest.param(GRANTER, "grant", GRANTER, "reader", set(), REFUSED, set(), id="a-grant-may-not-raise-the-callers-own-access"),
    ],
)
def test_a_rung_is_granted_revoked_and_listed_only_by_who_may_manage_its_grants(
    estate: Any, caller: str, door: str, grantee: str, relation: str, holding: set[str], answered: Answered, after: set[str]
) -> None:
    estate.store.write(*[(estate.person(grantee), held, f"warehouse:{WAREHOUSE}") for held in holding])
    body = {"user": estate.person(grantee), "relation": relation} if door != "list" else {}

    answer = estate.client.post(f"/management/v1/warehouse/{WAREHOUSE}/access/{door}", json=body, headers=estate.bearer(caller))

    assert (_answered(door, answer), _held(estate, grantee)) == (answered, after), answer.text
