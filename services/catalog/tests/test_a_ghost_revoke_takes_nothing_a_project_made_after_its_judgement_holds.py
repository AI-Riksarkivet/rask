"""The drift repair's ghost revoke takes nothing a project made after its judgement holds (ADR 0245, `Grants.tla`).

`POST /management/v1/grants/ghosts/revoke` revokes every grant on an object no registry names, judging each object again
on the catalog's own reads. A ghost on a project is a tuple on a creation that ended: its uuid no record names. A
project made under the same id between that judgement and the revoke is another creation, another object, so the revoke
takes nothing of it, whatever the ghost held (its creator's admin among it): `OwnedWhileLive`.

The repair is held after its judgement while the project is made; what its creator holds is read over HTTP at the estate
admin's check door. Against the catalog as deployed, a loopback Dex and a real OpenFGA ([[LH-235]]).
"""

from __future__ import annotations

import threading
from typing import Any

from catalog.services import grant_reconcile, tenancy


PROJECT = "acme"
#: the estate admin who makes the project; the maintenance identity the repair is asked by.
ALICE, MAINTENANCE = "alice", "maintenance"
#: An ended creation of the project: what an earlier project's door left stands on it.
GHOST = f"project:{'d' * 32}"
WAIT = 30


def test_a_project_made_while_its_ghost_is_revoked_keeps_its_admin(governed_catalog: Any, monkeypatch: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog and pytest's monkeypatch
    catalog = governed_catalog
    catalog.store.write(
        (catalog.person(ALICE), "admin", "estate:rask"),
        (catalog.person(MAINTENANCE), "reconciler", "estate:rask"),
        (catalog.person(ALICE), "admin", GHOST),
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
            catalog.client.post("/management/v1/grants/ghosts/revoke", json={"objects": [GHOST]}, headers=catalog.bearer(MAINTENANCE))
        )
    )
    repair.start()
    assert judged.wait(WAIT), "the repair never judged the object"
    made = catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ALICE))
    release.set()
    repair.join(WAIT)

    settings = catalog.client.app.state.settings
    live = tenancy.resolve(settings.registry_root, settings.storage_options(), "project", PROJECT)
    assert made.status_code == 200 and live is not None, made.text
    held = catalog.client.post(
        "/management/v1/access/check", json={"user": catalog.person(ALICE), "relation": "can_administer", "object": live.obj}, headers=catalog.bearer(ALICE)
    )
    assert held.status_code == 200, held.text
    assert answered and answered[0].json()["revoked"] == {GHOST: 1}, answered[0].text if answered else None
    assert held.json()["allowed"], "the project alice made as the repair ran has no admin"
