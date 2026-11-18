"""Grants on one project never refuse each other (ADR 0245, `GrantRefusedOnlyByDelete`).

A grant resolves the project's id to its live creation, asks its gate there and writes its own tuple in one Write. It
deletes nothing and moves no shared token, so grants of different tuples share nothing for the store to refuse one of
them over: only a delete of the project, beside it, makes a door answer otherwise.

Driven over HTTP against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA on a store of
the test's own, sixteen members doors at once.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any


PROJECT, ALICE = "acme", "alice"
GRANTS = 16


def test_sixteen_grants_on_one_project_at_once_all_land(governed_catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    catalog = governed_catalog
    catalog.store.write((catalog.person(ALICE), "admin", "estate:rask"))
    assert catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ALICE)).status_code == 200
    members = f"/management/v1/projects/{PROJECT}/members"
    headers = catalog.bearer(ALICE)

    def _grant(person: int) -> int:
        return catalog.client.put(members, json={"user": f"dex~p{person}", "relation": "member"}, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=GRANTS) as pool:
        answered = list(pool.map(_grant, range(GRANTS)))
    listed = catalog.client.get(members, headers=headers)

    assert answered == [200] * GRANTS
    assert len([m for m in listed.json()["members"] if m["relation"] == "member"]) == GRANTS, listed.text
