"""A scratch tenant on the live estate, and the two Dex users a live drill plays its parts with.

The estate admin creates a project, a warehouse with its own bucket and a bronze namespace in it, owns what it seeds there,
and grants the job identity the relations a drill names on that namespace. The job is never an owner. Everything is
deleted again at the end: the tables first, then the warehouse with its bucket, then the project.
"""

import uuid
from collections.abc import Callable, Iterator, Sequence

import requests
from byo_scenario import ADMIN_USER, CATALOG, PIPELINE_USER, Identity, ok


class Tenant:
    """The scratch tenant one run drives, and what it must take down again."""

    def __init__(self, purpose: str, grants: Sequence[str]) -> None:
        self.admin = Identity.person(ADMIN_USER)
        self.job = Identity.person(PIPELINE_USER)
        self.project = f"{purpose}-{uuid.uuid4().hex[:8]}"
        #: The tenant's namespace, in its own warehouse (whose id is the project's), so no other tenant's names meet it.
        self.bronze = f"{self.project}$bronze"
        self.grants = tuple(grants)
        self.tables: list[str] = []

    def create(self) -> None:
        admin = self.admin
        ok(admin.http.post(f"{CATALOG}/management/v1/projects", json={"id": self.project}))
        ok(admin.http.post(f"{CATALOG}/management/v1/warehouses", json={"id": self.project, "project": self.project}))
        ok(admin.http.post(f"{CATALOG}/v1/namespace/{self.bronze}/create", json={}))
        for relation in self.grants:
            ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{self.bronze}/access/grant", json={"user": self.job.principal, "relation": relation}))

    def tear_down(self) -> list[str]:
        """Drop every table, then the warehouse with its bucket, then the project; what failed."""
        failed: list[str] = []
        attempts: list[tuple[str, Callable[[], requests.Response]]] = [
            (table, lambda t=table: self.admin.http.post(f"{CATALOG}/v1/table/{t}/drop", json={}, timeout=60)) for table in self.tables
        ]
        attempts.append(
            (
                "warehouse",
                lambda: self.admin.http.delete(
                    f"{CATALOG}/management/v1/warehouses/{self.project}", params={"cascade": "true", "purge_bucket": "true"}, timeout=120
                ),
            )
        )
        attempts.append(("project", lambda: self.admin.http.delete(f"{CATALOG}/management/v1/projects/{self.project}", timeout=60)))
        for what, attempt in attempts:
            answer = attempt()
            if answer.status_code != 200:
                failed.append(f"{what}: {answer.status_code} {answer.text[:200]}")
        return failed


def provisioned(purpose: str, grants: Sequence[str]) -> Iterator[Tenant]:
    """A created tenant for a module fixture to yield, taken down afterwards whatever the drill did; a remnant fails."""
    made = Tenant(purpose, grants)
    try:
        made.create()
        yield made
    finally:
        left = made.tear_down()
        assert not left, f"the scratch tenant {made.project} was not fully removed: {left}"


def tuples_on(identity: Identity, obj: str) -> list[tuple[str, str]]:
    """Every tuple the store holds on ``obj``, as ``(user, relation)`` in order, as ``identity`` reads them through the
    catalog's tuple door, page by page: a reading a refused call must leave the same."""
    found: list[tuple[str, str]] = []
    params = {"object": obj}
    while True:
        page = ok(identity.http.get(f"{CATALOG}/management/v1/access/tuples", params=params, timeout=60))
        found += [(str(held["user"]), str(held["relation"])) for held in page["tuples"]]
        if not page.get("continuation"):
            return sorted(found)
        params["continuation"] = str(page["continuation"])
