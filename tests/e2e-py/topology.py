"""Create a top-level namespace through whichever door THIS estate admits.

Which door is right is a property of the estate, not of a suite. With `catalog.warehouses.enabled`
on, `POST /v1/namespace/{name}/create` answers 400 `top-level namespace '<n>' must belong to a
warehouse` — a TOPOLOGY refusal raised by `require_warehouse_scoped`, not an authorization one — and
the namespace has to arrive through its warehouse instead.

FOUR SUITES CARRIED THE SAME DRIFT and were found together on 2026-09-06, once the live runner stopped
withholding the env that had been making them skip. Each fired the root door, DISCARDED the response,
and then failed further down on a parent that had never been created: `test_governance_e2e` read the
400 as a governance defect, `test_client_direct_e2e` and `test_multibase_e2e` got 403
`can_create_table required on namespace:<n>` — a refusal on a namespace that does not exist — and
`test_observability_e2e` errored in fixture setup. Only `test_auth_e2e` had been migrated, by hand,
on 2026-08-25.

So the door choice lives in ONE place. A copy per suite is what let one migration happen and four not,
and `tests/e2e-py/conftest.py` puts this directory on `sys.path` precisely so a flat helper beside a
suite is importable by name.

`adopt_existing` is what makes it idempotent across re-runs against a long-lived estate: these suites
are written to be run repeatedly against a deployed release, not against a fresh one.
"""

from __future__ import annotations

import os

import requests


#: The warehouse to nest a suite's top-level namespace under. Empty means the estate serves no
#: warehouses, which is what `catalog.warehouses.enabled=false` looks like — then the root door is
#: correct and is what the catalog expects. `scripts/e2e_live.sh` discovers this from the deployed
#: catalog rather than hardcoding it.
WAREHOUSE = os.environ.get("LANCE_E2E_WAREHOUSE", "")


#: WHO IS ACTUALLY AN OUTSIDER HERE, for the legs that assert a 403 on someone with no grant.
#:
#: `bob@example.com` is not one on this estate and three suites assumed he was. `team:eng` is bound to
#: `project:acme`, and `project.admin` is "[user, role#assignee] or member from team" — so a team member
#: IS a project admin. Measured against the live OpenFGA store 2026-09-06:
#:   can_administer(user:bob,       project:acme) -> True
#:   can_administer(user:publisher, project:acme) -> False
#:
#: An outsider who is secretly privileged does not make a 403 leg fail honestly — it makes it allege a
#: governance hole that is not there, which this estate has now nearly filed twice. Worse, a leg can go
#: GREEN for the wrong reason: `test_commit_is_governed` passed only because its namespace had never
#: been created, so bob's commit 403'd on a missing parent rather than on a missing grant.
#:
#: `scripts/e2e_live.sh` chooses the same way — it ASKS the store which candidate is non-admin rather
#: than naming one — and `LANCE_E2E_OUTSIDER` overrides where an estate's outsider is someone else.
OUTSIDER = os.environ.get("LANCE_E2E_OUTSIDER", "publisher@rask.internal")


def create_top_level(server: str, name: str, headers: dict[str, str], *, timeout: float = 15.0) -> requests.Response:
    """POST the create, through the warehouse door when the estate has one."""
    if WAREHOUSE:
        return requests.post(
            f"{server.rstrip('/')}/v1/warehouses/{WAREHOUSE}/namespaces",
            headers=headers,
            json={"namespace": name, "adopt_existing": True},
            timeout=timeout,
        )
    return requests.post(f"{server.rstrip('/')}/v1/namespace/{name}/create", headers=headers, json={}, timeout=timeout)


def assert_parent_exists(response: requests.Response, name: str) -> None:
    """A create whose response nobody reads is how three suites failed on a missing parent.

    409 is success here — the namespace already exists from a previous run against this estate, which
    is the normal case and the reason `adopt_existing` is set.
    """
    assert response.status_code in (200, 201, 409), (
        f"could not create the top-level namespace {name!r} this suite nests under: "
        f"{response.status_code} {response.text[:400]}"
    )
