"""A project's deletion protection is set and cleared through its own door, by its admins alone ([[LH-549]]).

`POST /management/v1/projects/{project_id}/protection` takes `{"protected": true}` or `{"protected": false}`, the shape of
the table, namespace and warehouse doors. Protection is a statement about the project's destruction, so the door asks
what the delete it guards asks, `can_administer` on the project. A member of the project and the estate's owner, who
administers no project by that alone, are each answered the not-found a missing project gets, so the door tells nobody
which ids exist. The project's read answers the flag and who armed it, and its delete refuses a protected project
unless it is forced.

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's
own ([[LH-235]]).
"""

from __future__ import annotations

from typing import Any

import pytest

from service_kit.naming import CONTROL_ID_RE


PROJECT = "acme"
#: acme's admin, who made it as the estate's owner; a second admin of acme; a member of acme; another owner of the estate.
ADMIN, CO_ADMIN, MEMBER, ESTATE_OWNER = "alice", "dave", "carol", "erin"
#: What a refused request is answered: its status, media type, spec code (`lance_docs/ns_catalog/namespace/operations/errors.md`)
#: and detail. A caller who may not administer acme is answered exactly what a missing project is.
NOT_FOUND = (404, "application/problem+json", 4, f"project not found: {PROJECT}")
#: What a protected project's delete is answered without `force=true`: the remedy, named.
REFUSED_UNLESS_FORCED = (
    f"project '{PROJECT}' is protected against deletion. Pass force=true to override "
    "(protection only — the same authorization is still required), or clear the flag first."
)


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """acme, made through its door by alice, who owns the estate as the chart's bootstrap hook grants it, with dave a
    second admin of acme, carol a member of it, and erin another owner of the estate."""
    catalog = governed_catalog
    catalog.store.write((catalog.person(ADMIN), "owner", "estate:rask"), (catalog.person(ESTATE_OWNER), "owner", "estate:rask"))
    made = catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ADMIN))
    assert made.status_code == 200, made.text
    catalog.store.write(
        (catalog.person(CO_ADMIN), "admin", f"project:{PROJECT}"),
        (catalog.person(MEMBER), "member", f"project:{PROJECT}"),
    )
    return catalog


def _protection_of(estate: Any) -> tuple[bool, str | None]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """acme's protection and who armed it, as its read answers them to the estate's owner."""
    read = estate.client.get(f"/management/v1/projects/{PROJECT}", headers=estate.bearer(ADMIN))
    assert read.status_code == 200, read.text
    return read.json()["protected"], read.json().get("protected_by")


@pytest.mark.parametrize(
    ("armed", "caller", "project", "protected", "answered", "after"),
    [
        pytest.param(
            False,
            ADMIN,
            "Bad_Id",
            True,
            (400, "application/problem+json", 13, f"invalid project id 'Bad_Id': must match {CONTROL_ID_RE.pattern}"),
            (False, None),
            id="an-id-no-project-can-hold-is-refused",
        ),
        pytest.param(False, ADMIN, "ghost", True, (404, "application/problem+json", 4, "project not found: ghost"), (False, None), id="a-missing-project"),
        pytest.param(False, MEMBER, PROJECT, True, NOT_FOUND, (False, None), id="a-member-is-refused"),
        pytest.param(False, ESTATE_OWNER, PROJECT, True, NOT_FOUND, (False, None), id="the-estates-owner-is-refused"),
        pytest.param(False, ADMIN, PROJECT, True, (200, {"id": PROJECT, "protected": True}), (True, ADMIN), id="an-admin-arms-it"),
        pytest.param(True, CO_ADMIN, PROJECT, True, (200, {"id": PROJECT, "protected": True}), (True, ADMIN), id="re-arming-keeps-the-armer"),
        pytest.param(True, CO_ADMIN, PROJECT, False, (200, {"id": PROJECT, "protected": False}), (False, None), id="another-admin-clears-it"),
    ],
)
def test_only_its_admins_arm_and_clear_it(
    estate: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    armed: bool,
    caller: str,
    project: str,
    protected: bool,
    answered: tuple[Any, ...],
    after: tuple[bool, str | None],
) -> None:
    if armed:
        assert estate.client.post(f"/management/v1/projects/{PROJECT}/protection", json={"protected": True}, headers=estate.bearer(ADMIN)).status_code == 200

    answer = estate.client.post(f"/management/v1/projects/{project}/protection", json={"protected": protected}, headers=estate.bearer(caller))

    if answer.status_code == 200:
        assert (answer.status_code, answer.json()) == answered
    else:
        assert (answer.status_code, answer.headers["content-type"], answer.json()["code"], answer.json()["detail"]) == answered
    flag, armer = after
    assert _protection_of(estate) == (flag, armer and estate.person(armer))


@pytest.mark.parametrize(
    ("force", "answered", "read_after"),
    [
        pytest.param("false", (409, "application/problem+json", 3, REFUSED_UNLESS_FORCED), 200, id="refused-unless-forced"),
        pytest.param("true", (200, {"project": PROJECT, "tuples_revoked": 3}), 404, id="forced"),
    ],
)
def test_a_protected_projects_delete_is_refused_unless_forced(estate: Any, force: str, answered: tuple[Any, ...], read_after: int) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    armed = estate.client.post(f"/management/v1/projects/{PROJECT}/protection", json={"protected": True}, headers=estate.bearer(CO_ADMIN))
    assert armed.status_code == 200, armed.text

    answer = estate.client.delete(f"/management/v1/projects/{PROJECT}", params={"force": force}, headers=estate.bearer(ADMIN))

    if answer.status_code == 200:
        assert (answer.status_code, answer.json()) == answered
    else:
        assert (answer.status_code, answer.headers["content-type"], answer.json()["code"], answer.json()["detail"]) == answered
    assert estate.client.get(f"/management/v1/projects/{PROJECT}", headers=estate.bearer(ADMIN)).status_code == read_after
