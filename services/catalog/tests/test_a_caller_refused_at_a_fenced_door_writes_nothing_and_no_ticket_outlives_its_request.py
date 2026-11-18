"""A caller refused at a door writing on a project writes nothing there, and no ticket outlives the request that wrote
it (ADR 0241 §8 B2, `specs/catalog/Grants.tla`).

A door writing on or naming a project opens a fence, a ticket of its own naming the project. It used to open it before
it asked its caller's authority, so a caller with no right on another tenant's project wrote there, paid a paginated
read of it, and left its ticket standing whenever the request died before taking it back (`GrantsIntruderFenced`,
`GrantsTicketsLeft`). The door now asks first with no write, opens its fence, and asks again on the incarnation it read
(``fga_deps.door_fence``); the fence is a context manager that takes its ticket back on every exit (``fga.fenced``); and
the converge tick sweeps a ticket older than a request runs (``incarnations.converge``).

Against the catalog as deployed: a loopback Dex signing callers in, a real OpenFGA deciding on a store of the test's own,
and the store's own changelog read for what each request wrote.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from functools import partial
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from lance_namespace import ServiceUnavailableError

from catalog.services import incarnations
from lakehouse.governed import fga


PROJECT, ALICE, MALLORY = "acme", "alice", "mallory"
MEMBERS, ACCESS, ROLES = f"/management/v1/projects/{PROJECT}/members", f"/management/v1/projects/{PROJECT}/access", f"/management/v1/projects/{PROJECT}/roles"

#: Each door writing on project acme, as the caller with no right on it asks it.
DOORS = {
    "member-grant": ("PUT", MEMBERS, {"user": "dex~bob", "relation": "member"}),
    "member-revoke": ("DELETE", MEMBERS, {"user": "dex~bob", "relation": "member"}),
    "access-grant": ("POST", f"{ACCESS}/grant", {"user": "dex~bob", "relation": "decider"}),
    "role-create": ("POST", ROLES, {"name": "intruders"}),
}


def _changes(catalog: Any) -> int:  # noqa: ANN401 — the conftest's GovernedCatalog
    """How many tuple changes the store's changelog holds on a project or a fence ticket."""
    total = 0
    for kind in ("project", fga.FENCE_TYPE):
        token = ""
        while True:
            page = httpx.get(f"{catalog.store.url}/stores/{catalog.store.store_id}/changes", params={"type": kind, "continuation_token": token}).json()
            if not page.get("changes"):
                break
            total += len(page["changes"])
            token = page["continuation_token"]
    return total


def _tickets(catalog: Any) -> list[fga.StoredTuple]:  # noqa: ANN401 — the conftest's GovernedCatalog
    app = catalog.client.app
    assert isinstance(app, FastAPI) and catalog.client.portal is not None
    return catalog.client.portal.call(fga.read_tickets, app.state.fga, f"project:{PROJECT}")


@pytest.fixture
def acme(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    catalog = governed_catalog
    catalog.store.write((catalog.person(ALICE), "admin", "estate:rask"))
    assert catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(ALICE)).status_code == 200
    return catalog


@pytest.mark.parametrize("door", list(DOORS))
def test_a_caller_the_project_authorizes_for_nothing_is_refused_having_written_nothing_there(acme: Any, door: str) -> None:  # noqa: ANN401 — the fixture's GovernedCatalog
    method, path, body = DOORS[door]
    before = _changes(acme)

    answered = acme.client.request(method, path, json=body, headers=acme.bearer(MALLORY))

    assert (answered.status_code, _changes(acme) - before) in {(403, 0), (404, 0)}, answered.text


@pytest.mark.parametrize("died", ["its-write-failed", "its-process-died"])
def test_no_ticket_outlives_the_request_that_wrote_it(acme: Any, monkeypatch: pytest.MonkeyPatch, died: str) -> None:  # noqa: ANN401 — the fixture's GovernedCatalog
    """A door whose write fails takes its ticket back as it answers; a ticket a dead process left, which nothing else
    takes back, stands until the converge tick finds it older than a request runs, and then it goes."""
    app = acme.client.app
    assert isinstance(app, FastAPI) and acme.client.portal is not None
    if died == "its-write-failed":

        async def _unanswered(*_args: Any, **_kwargs: Any) -> bool:  # noqa: ANN401 — commit_fenced's own arguments
            raise ServiceUnavailableError("authorization service unavailable")

        monkeypatch.setattr(fga, "commit_fenced", _unanswered)
        answered = acme.client.put(MEMBERS, json={"user": "dex~bob", "relation": "member"}, headers=acme.bearer(ALICE))
        assert (answered.status_code, _tickets(acme)) == (503, []), answered.text
        return
    acme.store.write((f"project:{PROJECT}", fga.TICKET, f"{fga.FENCE_TYPE}:{uuid.uuid4().hex}"))
    settings = app.state.settings
    converge = partial(incarnations.converge, settings.registry_root, settings.storage_options(), app.state.fga)

    acme.client.portal.call(partial(converge, bound=incarnations.ticket_bound(settings.request_deadline_seconds)))
    kept = len(_tickets(acme))
    swept = acme.client.portal.call(partial(converge, bound=timedelta(0)))

    assert (kept, swept.swept, _tickets(acme)) == (1, {f"project:{PROJECT}": 1}, [])
