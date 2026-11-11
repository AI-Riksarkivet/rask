"""A project is read by the callers its list shows it to ([[LH-587]]).

`GET /management/v1/projects` answers a project to an estate observer and to each of its members.
`GET /management/v1/projects/{project_id}` answers the same record to the same callers, so a project's member who observes
nothing estate-wide opens the project the list shows them, and the audit trail records each read as allowed on the object
that opened it: the project for a member, the estate for an observer. A caller the list shows nothing is answered what a
missing project answers, so the door tells nobody which ids exist, and the trail records the refusal on each object the
door asked about: the estate and the project.

On the catalog as deployed with authorization on: a loopback Dex signs the callers in and a real OpenFGA decides
(`governed_catalog`).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from lakehouse.governed.audit import AUDIT_LOGGER, AuditAction, AuditOutcome


PROJECT, ESTATE = "acme", "estate:rask"
#: The estate's owner, who made acme; a member of acme holding nothing on the estate; another owner of the estate, who
#: is no member of acme; and a signed-in person holding no grant.
OWNER, MEMBER, OBSERVER, STRANGER = "alice", "carol", "erin", "mallory"


@pytest.fixture
def estate(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """acme, made through its door by alice, who owns the estate as the chart's bootstrap hook grants it, with carol a
    member of it and erin another owner of the estate."""
    catalog = governed_catalog
    catalog.store.write((catalog.person(OWNER), "owner", ESTATE), (catalog.person(OBSERVER), "owner", ESTATE))
    made = catalog.client.post("/management/v1/projects", json={"id": PROJECT}, headers=catalog.bearer(OWNER))
    assert made.status_code == 200, made.text
    catalog.store.write((catalog.person(MEMBER), "member", f"project:{PROJECT}"))
    return catalog


def _answer(response: httpx.Response) -> tuple[int, str, int, str]:
    """A refusal as its caller reads it: status, media type, spec code (`lance_docs/ns_catalog/namespace/operations/errors.md`)
    and detail."""
    body = response.json()
    return response.status_code, response.headers["content-type"], body["code"], body["detail"]


def _recorded(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str, str, str]]:
    """Every audit record the door wrote, as its action, outcome, relation and object: all but authentication's, which
    every request writes before any door runs."""
    return sorted(
        (record.__dict__["audit.action"], record.__dict__["audit.outcome"], record.__dict__.get("audit.relation", ""), record.__dict__["audit.resource"])
        for record in caplog.records
        if record.name == AUDIT_LOGGER and record.__dict__.get("audit.action") != AuditAction.AUTHN
    )


@pytest.mark.parametrize(
    ("caller", "opened_by"),
    [
        pytest.param(MEMBER, ("member", f"project:{PROJECT}"), id="a-member-who-observes-nothing-estate-wide"),
        pytest.param(OBSERVER, ("can_observe_events", ESTATE), id="an-estate-observer-who-is-no-member"),
    ],
)
def test_a_project_is_read_by_a_caller_its_list_shows_it_to(
    estate: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    caplog: pytest.LogCaptureFixture,
    caller: str,
    opened_by: tuple[str, str],
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    listed = estate.client.get("/management/v1/projects", headers=estate.bearer(caller))
    assert listed.status_code == 200, listed.text
    caplog.clear()

    read = estate.client.get(f"/management/v1/projects/{PROJECT}", headers=estate.bearer(caller))

    assert read.status_code == 200, read.text
    assert read.json() == next(listing for listing in listed.json() if listing["project"] == PROJECT)
    assert _recorded(caplog) == [(AuditAction.AUTHZ, AuditOutcome.ALLOW, *opened_by)]


def test_a_caller_the_list_shows_nothing_is_answered_what_a_missing_project_answers(
    estate: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    missing = estate.client.get("/management/v1/projects/ghost", headers=estate.bearer(OBSERVER))
    caplog.clear()

    refused = estate.client.get(f"/management/v1/projects/{PROJECT}", headers=estate.bearer(STRANGER))

    assert _answer(missing) == (404, "application/problem+json", 4, "project not found: ghost")
    assert _answer(refused) == (404, "application/problem+json", 4, f"project not found: {PROJECT}")
    assert _recorded(caplog) == [
        (AuditAction.AUTHZ, AuditOutcome.DENY, "can_observe_events", ESTATE),
        (AuditAction.AUTHZ, AuditOutcome.DENY, "member", f"project:{PROJECT}"),
    ]
