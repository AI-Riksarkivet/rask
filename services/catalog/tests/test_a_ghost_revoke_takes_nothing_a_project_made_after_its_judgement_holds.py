"""The drift repair's ghost revoke takes nothing a project made after its judgement holds (ADR 0241, `Grants.tla`).

`POST /management/v1/grants/ghosts/revoke` revokes every grant on an object no registry names, judging each object again
on the catalog's own reads. A project's id may be held again once its project is deleted, so a project can be made under
it between that judgement and the revoke. The model's trace (`GrantsRepairJudgesFirst`): the repair judges the record
absent, a create mints the record and bears the project with its creator's admin grant, and the revoke, reading what
stands only then, takes that grant with the ghost's: the new tenant stands with nobody to administer, grant on or delete
it.

The ghost is parametrized: another person's grant, and a grant equal to the seed the project is born with (its creator's
admin, left by an earlier project under the id; `GrantsRepairUntokened`): the birth skips a tuple that already stands, so
a repair that revokes what it read would take the new project's only admin. The repair writes a token of its own before
it reads, and the birth deletes it, so the revoke commits only while nothing was born since its read.

The repair is held after its judgement while the project is made; what its creator holds is read over HTTP at the estate
admin's check door. Against the catalog as deployed, a loopback Dex and a real OpenFGA ([[LH-235]]).
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from catalog.services import grant_reconcile


PROJECT = "acme"
#: the estate admin who makes the project; the maintenance identity the repair is asked by; who the ghost grant names.
ALICE, MAINTENANCE, BOB = "alice", "maintenance", "bob"
WAIT = 30


@pytest.mark.parametrize(("ghost", "relation"), [pytest.param(BOB, "member", id="another-persons-grant"), pytest.param(ALICE, "admin", id="the-seed-itself")])
def test_a_project_made_while_its_ghost_is_revoked_keeps_its_admin(governed_catalog: Any, monkeypatch: Any, ghost: str, relation: str) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog and pytest's monkeypatch
    catalog = governed_catalog
    catalog.store.write(
        (catalog.person(ALICE), "admin", "estate:rask"),
        (catalog.person(MAINTENANCE), "reconciler", "estate:rask"),
        # What an earlier project's door left: a tuple on the id no record names.
        (catalog.person(ghost), relation, f"project:{PROJECT}"),
    )
    judged, release, real_judge = threading.Event(), threading.Event(), grant_reconcile.ghost_refusal

    def _judge(*args: Any) -> str | None:  # noqa: ANN401 — ghost_refusal's own arguments
        reason = real_judge(*args)
        judged.set()
        assert release.wait(WAIT), "the held repair was never released"
        return reason

    monkeypatch.setattr(grant_reconcile, "ghost_refusal", _judge)
    answered: list[Any] = []
    repair = threading.Thread(
        target=lambda: answered.append(
            catalog.client.post("/management/v1/grants/ghosts/revoke", json={"objects": [f"project:{PROJECT}"]}, headers=catalog.bearer(MAINTENANCE))
        )
    )
    repair.start()
    assert judged.wait(WAIT), "the repair never judged the object"
    made = catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ALICE))
    release.set()
    repair.join(WAIT)

    held = catalog.client.post(
        "/management/v1/access/check",
        json={"user": catalog.person(ALICE), "relation": "can_administer", "object": f"project:{PROJECT}"},
        headers=catalog.bearer(ALICE),
    )
    assert (made.status_code, held.status_code) == (200, 200), (made.text, held.text)
    assert held.json()["allowed"], (
        f"the repair answered {answered[0].status_code if answered else None}: {answered[0].text if answered else ''}, "
        "and the project alice made as it ran has no admin"
    )
