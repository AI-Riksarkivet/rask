"""P6 — the tenant membership API (``/management/v1/projects/{id}/members``).

The gap: ``project#admin``/``member`` were writable only by ``seed_project_admin`` at create time and
by the estate-gated raw tuple editor, so a project admin could not invite anyone into their own
tenant. These pin how the grant door writes and who may make it write. The property that makes
``can_grant_admin: admin`` safe to ship, that a project always keeps an administrator, is driven against a real
OpenFGA in ``services/catalog/tests/test_a_project_always_keeps_an_administrator.py``.

On the catalog as deployed with authorization on (``governed_catalog``): root, the estate admin, creates ``acme`` and so
administers it, and every tuple is read back off the real store.
"""

from typing import Any

import pytest


MEMBERS = "/management/v1/projects/acme/members"


@pytest.fixture
def acme(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """``acme``, made through its door by root, who holds the estate's admin as the catalog's declaration grants it."""
    catalog = governed_catalog
    catalog.store.write((catalog.person("root"), "admin", "estate:rask"))
    assert catalog.client.post("/management/v1/projects", json={"id": "acme"}, headers=catalog.bearer("root")).status_code == 200
    return catalog


@pytest.mark.parametrize(
    ("caller", "status", "lands"),
    [
        pytest.param("root", 200, True, id="the-projects-admin-grants-and-a-bare-subject-is-the-user-it-names"),
        pytest.param("mallory", 403, False, id="a-caller-who-administers-nothing-is-refused-and-writes-nothing"),
    ],
)
def test_the_grant_door_writes_only_for_the_projects_admin(acme: Any, caller: str, status: int, lands: bool) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """The door asks ``can_grant_member`` on the project, so mallory is refused 403 code 15 and the store is left as it
    was, and root's grant of the bare ``dex~bob`` is written as the one user it names and listed back."""
    before = acme.store.tuples()

    answered = acme.client.put(MEMBERS, json={"user": "dex~bob", "relation": "member"}, headers=acme.bearer(caller))

    assert answered.status_code == status, answered.text
    granted = ("user:dex~bob", "member", "project:acme")
    assert acme.store.tuples() - before == ({granted} if lands else set())
    if lands:
        assert {"user": "user:dex~bob", "relation": "member"} in answered.json()["members"]
    else:
        assert answered.json()["code"] == 15, answered.text


def test_a_userset_is_granted_verbatim_and_a_held_grant_again_is_a_noop(acme: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """`role:x#assignee` must NOT become `user:role:x#assignee`, a subject that matches no tuple. And re-granting what
    someone already holds is answered as the first grant was and leaves the store as it is."""
    before = acme.store.tuples()
    for _ in range(2):
        for user in ("role:curators#assignee", "dex~bob"):
            answered = acme.client.put(MEMBERS, json={"user": user, "relation": "member"}, headers=acme.bearer("root"))
            assert answered.status_code == 200, answered.text

    assert acme.store.tuples() - before == {("role:curators#assignee", "member", "project:acme"), ("user:dex~bob", "member", "project:acme")}
