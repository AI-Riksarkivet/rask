"""Create a suite's namespace in the estate's warehouse, through the spec's own door, and drop it again at session end.

Every name's first segment is its warehouse ([[LH-593]]), so a namespace a suite nests its tables under is
`<WAREHOUSE>$<name>`, created with `POST /v1/namespace/<WAREHOUSE>$<name>/create`. The door choice lives in ONE place:
a copy per suite is what let one migration land and four not (2026-09-06), and `tests/e2e-py/conftest.py` puts this
directory on `sys.path` so a flat helper beside a suite is importable by name.

The create is idempotent across re-runs against a long-lived estate: these suites run repeatedly against a deployed
release, so a 409 (`NamespaceAlreadyExists`, the spec's default `Create` mode) means the namespace is already there
and the suite proceeds in it.
"""

from __future__ import annotations

import os

import requests


#: The warehouse a suite's namespaces are created in: the first segment of each name. `scripts/e2e_live.sh` discovers it
#: from the deployed catalog (the project's oldest active warehouse, else the estate's own) and `scripts/e2e_stack.sh`
#: names the one it makes. Unset, it is the estate's own warehouse at the chart's default id, which a lane that runs the
#: catalog straight from its settings (`dagger call governance-chain`) serves.
WAREHOUSE = os.environ.get("LANCE_E2E_WAREHOUSE") or "lance_catalog"


#: WHO IS ACTUALLY AN OUTSIDER HERE, for the legs that assert a 403 on someone with no grant.
#:
#: `bob@example.com` is not one on this estate and three suites assumed he was. The seed makes him an
#: assignee of acme's admins role, which holds `admin` on `project:acme` — so bob IS a project admin.
#: Measured against the live OpenFGA store 2026-09-06:
#:   can_administer(user:bob,       project:acme) -> True
#:   can_administer(user:publisher, project:acme) -> False
#:
#: An outsider who is secretly privileged does not make a 403 leg fail honestly — it makes it allege a
#: governance hole that is not there, which this estate has now nearly filed twice. Worse, a leg can go
#: GREEN for the wrong reason: a commit leg passed only because its namespace had never been
#: created, so bob's commit 403'd on a missing parent rather than on a missing grant.
#:
#: `scripts/e2e_live.sh` chooses the same way — it ASKS the store which candidate is non-admin rather
#: than naming one — and `LANCE_E2E_OUTSIDER` overrides where an estate's outsider is someone else.
#:
#: THE DEFAULT IS THE CHART-DEPLOYED ESTATE'S, and one estate does not take it: the hermetic Dagger
#: stack (`dagger call governance-chain`) mounts `.docker/dex.config.yaml`, which knows alice and bob
#: and nobody else, and seeds neither acme's admins role nor `project:acme` — so bob is a genuine outsider
#: there and `publisher@rask.internal` is not an identity at all. That lane sets the override, and
#: the override and that Dex config are a pair: repointing either one alone leaves the lane minting a
#: token for a user its Dex does not have.
OUTSIDER = os.environ.get("LANCE_E2E_OUTSIDER", "publisher@rask.internal")


#: What THIS process created, so the session teardown can drop it — `(server, namespace id, headers)`.
#:
#: RECORDED HERE because this is the one door every suite goes through, and cleaning up at the seam
#: that creates is the only way that does not need every suite retrofitted. Only a genuine CREATE is
#: recorded: a 409 means the namespace was already there, and removing something this run did not make
#: is how a cleanup becomes the incident.
CREATED: list[tuple[str, str, dict[str, str]]] = []


def namespace_id(name: str) -> str:
    """The full id of the namespace ``name`` in the suites' warehouse: `<WAREHOUSE>$<name>`."""
    return f"{WAREHOUSE}${name}"


def create_namespace(server: str, name: str, headers: dict[str, str], *, timeout: float = 15.0) -> str:
    """Create `<WAREHOUSE>$<name>` through the spec's create door, REMEMBER a genuine create, and return its full id.

    200 is a create and 409 is a namespace an earlier run left, kept as it is; any other answer fails here, naming it,
    because a create whose answer nobody reads is how three suites failed further down on a parent that was never
    there.
    """
    made = namespace_id(name)
    response = requests.post(f"{server.rstrip('/')}/v1/namespace/{made}/create", headers=headers, json={}, timeout=timeout)
    assert response.status_code in (200, 409), f"could not create the namespace {made!r} this suite nests under: {response.status_code} {response.text[:400]}"
    if response.status_code == 200:
        CREATED.append((server, made, dict(headers)))
    return made


def drop_created(*, timeout: float = 15.0) -> list[str]:
    """Drop what this run created; return a line per namespace that could NOT be removed.

    THE REFUSALS ARE THE POINT, not an error to swallow. The drop is the spec's default `Restrict`, so a namespace still
    holding tables answers 409 naming them, and that is correct: this cleanup does not take a suite's tables with it. An
    empty one is purged, so no trash record holds its name for the grace window after the run. So
    this reports what it leaves behind instead of claiming a clean run, because residue nobody is told about is how
    § Q8-15 accumulated 1,163 dataset nodes.
    """
    left: list[str] = []
    while CREATED:
        server, made, headers = CREATED.pop()
        try:
            response = requests.post(f"{server.rstrip('/')}/v1/namespace/{made}/drop", headers=headers, params={"purge": "true"}, json={}, timeout=timeout)
        except requests.RequestException as exc:  # noqa: PERF203 — one unreachable catalog must not hide the rest
            left.append(f"{made}: unreachable ({type(exc).__name__})")
            continue
        if response.status_code not in (200, 404):
            left.append(f"{made}: {response.status_code} {response.text[:160]}")
    return left
