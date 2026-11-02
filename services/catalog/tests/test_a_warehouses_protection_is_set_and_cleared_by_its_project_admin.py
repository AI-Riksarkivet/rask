"""A warehouse's deletion protection is set and cleared through its own door, by its project's admins alone ([[LH-548]]).

`POST /management/v1/warehouses/{warehouse_id}/protection` takes `{"protected": true}` or `{"protected": false}`, the
table and namespace doors' shape. Protection is a statement about the warehouse's destruction, so the door asks what the
delete it guards asks, `can_administer` on the warehouse's project. The warehouse's own owner and a member of its project
hold no such rung, and each is answered the not-found a missing warehouse gets, so the door tells nobody which ids exist.
The record names who armed it until it is cleared, and another admin arming it again leaves that name as it is.

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
#: acme's admin, who made it; a second admin of acme; the warehouse's direct owner; a member of acme.
ADMIN, CO_ADMIN, OWNER, MEMBER = "alice", "dave", "bob", "carol"
#: The spec code a missing warehouse is answered with (`lance_docs/ns_catalog/namespace/operations/errors.md`).
TABLE_NOT_FOUND = 4


@pytest.fixture
def estate(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Any:
    """acme, made through its door by alice, who owns the estate as the chart's bootstrap hook grants it, and acme's
    warehouse, written where its door keeps it (the door provisions a bucket and probes the store), with dave a second
    admin of acme, bob the warehouse's owner and carol a member of acme."""
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
        (catalog.person(CO_ADMIN), "admin", f"project:{PROJECT}"),
        (catalog.person(OWNER), "owner", f"warehouse:{WAREHOUSE}"),
        (catalog.person(MEMBER), "member", f"project:{PROJECT}"),
    )
    return catalog


def test_its_project_admins_arm_and_clear_it_and_anyone_else_is_answered_not_found(estate: Any) -> None:
    client = estate.client

    def protect(caller: str, protected: bool) -> tuple[int, Any, bool | None, str | None]:
        """What ``caller`` is answered, and the warehouse's protection afterwards, as its admin reads it."""
        answer = client.post(f"/management/v1/warehouses/{WAREHOUSE}/protection", json={"protected": protected}, headers=estate.bearer(caller))
        read = client.get(f"/management/v1/warehouses/{WAREHOUSE}", headers=estate.bearer(ADMIN))
        assert read.status_code == 200, read.text
        body, after = answer.json(), read.json()
        return answer.status_code, body.get("code", body), after.get("protected"), after.get("protected_by")

    answers = [
        protect(OWNER, True),
        protect(MEMBER, True),
        protect(ADMIN, True),
        protect(CO_ADMIN, True),
        protect(OWNER, False),
        protect(CO_ADMIN, False),
    ]

    armed_by = estate.person(ADMIN)
    assert answers == [
        (404, TABLE_NOT_FOUND, None, None),
        (404, TABLE_NOT_FOUND, None, None),
        (200, {"id": WAREHOUSE, "protected": True}, True, armed_by),
        (200, {"id": WAREHOUSE, "protected": True}, True, armed_by),
        (404, TABLE_NOT_FOUND, True, armed_by),
        (200, {"id": WAREHOUSE, "protected": False}, None, None),
    ]
