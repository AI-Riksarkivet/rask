"""One estate-admin door removes a departed principal from every grant and records the principal deleted ([[LH-233]]).

A person's grants are tuples naming their principal on objects of every type the model defines, written by many doors
(the members door, the per-object grant doors, the raw tuple door), so offboarding was a hunt through the store one
tuple at a time. ``DELETE /management/v1/access/principals/{principal}`` reads every tuple naming the principal, revokes
them, records the principal deleted under the control root and announces ``principal_deleted`` for the notifications
plane to erase the person's inbox (CTL-022); ``GET`` on the same path answers the record. It never strands a project:
a principal holding the only direct ``admin`` of any project is refused (409, code 14) and nothing is revoked, not even
an admin it holds on another project beside a second admin, and
an estate admin cannot delete themselves, since the rung the door asks would go with them. A principal whose grants git
declares is refused too, since the next upgrade or boot would grant it again: one holding a rung on the estate, which
only the chart's bootstrap hook grants, and an account the catalog's settings name.

Every decision is the real model's, on a store of this test's own. The estate admin's ``owner`` tuple on the estate is
seeded as the chart's bootstrap hook seeds it; everything else goes through the doors.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from catalog.api.dependencies import get_control_emitter
from catalog.core.config import get_settings
from service_kit.control_events import CatalogControlEvent
from service_kit.governed import fga


#: The Lance Namespace spec's error codes a stock client reads (`spec.yaml`).
INVALID_INPUT, CONCURRENT_MODIFICATION = 13, 14
PRINCIPALS = "/management/v1/access/principals"
ALICE = "dex~alice"


class _Recorder:
    """The control emitter, as a list of the events the door published."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)


def _ok(response: httpx.Response) -> httpx.Response:
    assert response.status_code == 200, response.text
    return response


@pytest.fixture
def estate(governed_catalog: Any) -> tuple[Any, _Recorder]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """root owns the estate and creates ``acme``; alice holds tuples through three doors, and bob is acme's second admin."""
    catalog = governed_catalog
    recorder = _Recorder()
    catalog.client.app.dependency_overrides[get_control_emitter] = lambda: recorder
    catalog.store.write((catalog.person("root"), "owner", "estate:rask"))
    _ok(catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=catalog.bearer("root")))
    for user, relation in (("dex~alice", "admin"), ("dex~bob", "admin"), ("dex~gina", "member")):
        _ok(catalog.client.put("/management/v1/projects/acme/members", json={"user": user, "relation": relation}, headers=catalog.bearer("root")))
    for relation, obj in (("member", "team:stewards"), ("curator", "classification:pii")):
        tuple_body = {"user": fga.user_object(ALICE), "relation": relation, "object": obj}
        _ok(catalog.client.post("/management/v1/access/tuples", json=tuple_body, headers=catalog.bearer("root")))
    return catalog, recorder


def _naming(catalog: Any, principal: str) -> set[tuple[str, str, str]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    return {t for t in catalog.store.tuples() if t[0] == fga.user_object(principal)}


def test_the_door_revokes_every_grant_naming_the_principal_and_records_it_deleted(estate: tuple[Any, _Recorder]) -> None:
    catalog, recorder = estate
    before = catalog.store.tuples()
    assert _naming(catalog, ALICE) == {
        (fga.user_object(ALICE), "admin", "project:acme"),
        (fga.user_object(ALICE), "member", "team:stewards"),
        (fga.user_object(ALICE), "curator", "classification:pii"),
    }

    deleted = _ok(catalog.client.delete(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))).json()

    assert _naming(catalog, ALICE) == set()
    assert {t for t in before if t[0] != fga.user_object(ALICE) and t[1] != fga.ADMIN_REVISION} <= catalog.store.tuples()
    record = _ok(catalog.client.get(f"{PRINCIPALS}/{ALICE}", headers=catalog.bearer("root"))).json()
    assert (record["principal"], record["kind"], record["deleted_by"], record["deleted_at"] is not None) == (ALICE, "person", "dex~root", True)
    assert deleted["revoked"] == 3
    assert [(e.action, e.object_type, e.object_id, e.extra["subject"]) for e in recorder.events if e.action == "principal_deleted"] == [
        ("principal_deleted", "principal", fga.user_object(ALICE), fga.user_object(ALICE))
    ]


@pytest.mark.parametrize(
    ("caller", "principal", "arrange", "answer"),
    [
        pytest.param("root", ALICE, "sole-admin-of-beta", (409, CONCURRENT_MODIFICATION), id="the-last-admin-of-one-of-its-projects"),
        pytest.param("carol", "dex~carol", "estate-admin-through-a-role", (400, INVALID_INPUT), id="the-estate-admin-themself"),
        pytest.param("root", ALICE, "an-estate-authority", (400, INVALID_INPUT), id="a-principal-the-bootstrap-hook-grants"),
        pytest.param("root", ALICE, "named-in-the-catalogs-settings", (400, INVALID_INPUT), id="an-account-the-catalog-seeds-from-its-settings"),
    ],
)
def test_a_deletion_that_would_strand_an_administration_or_undo_git_is_refused_whole(  # noqa: PLR0913 - parametrized
    estate: tuple[Any, _Recorder], monkeypatch: pytest.MonkeyPatch, caller: str, principal: str, arrange: str, answer: tuple[int, int]
) -> None:
    catalog, recorder = estate
    if arrange == "sole-admin-of-beta":
        # alice's admin on acme is revocable (bob is acme's other admin); on beta it is the only one.
        beta = "/management/v1/projects/beta/members"
        _ok(catalog.client.post("/management/v1/projects", json={"id": "beta"}, headers=catalog.bearer("root")))
        _ok(catalog.client.put(beta, json={"user": ALICE, "relation": "admin"}, headers=catalog.bearer("root")))
        _ok(catalog.client.request("DELETE", beta, json={"user": "dex~root", "relation": "admin"}, headers=catalog.bearer("root")))
    if arrange == "estate-admin-through-a-role":
        # carol holds the estate rung only through a role, so no tuple of hers is on the estate itself.
        catalog.store.write(("role:ops#assignee", "owner", "estate:rask"), (catalog.person("carol"), "assignee", "role:ops"))
    if arrange == "an-estate-authority":
        # As the chart's bootstrap hook grants a service an estate rung (`event_reader`, a decision role).
        catalog.store.write((catalog.person("alice"), "event_reader", "estate:rask"))
    if arrange == "named-in-the-catalogs-settings":
        # As the chart names a provenance writer, which the catalog grants at boot.
        monkeypatch.setenv("LANCE_PROVENANCE_WRITERS", json.dumps([ALICE]))
        get_settings.cache_clear()
    before = catalog.store.tuples()

    refused = catalog.client.delete(f"{PRINCIPALS}/{principal}", headers=catalog.bearer(caller))

    assert (refused.status_code, refused.json()["code"]) == answer, refused.text
    assert catalog.store.tuples() == before
    assert catalog.client.get(f"{PRINCIPALS}/{principal}", headers=catalog.bearer("root")).status_code == 404
    assert [e.action for e in recorder.events if e.action == "principal_deleted"] == []
