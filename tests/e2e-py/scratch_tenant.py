"""A scratch tenant on the live estate, and the two Dex users a live drill plays its parts with.

The estate admin creates a project, a warehouse with its own bucket and a bronze namespace, owns what it seeds there,
and grants the job identity the relations a drill names on that namespace. The job is never an owner. Everything is
deleted again at the end: the tables first, then the warehouse with its bucket, then the project.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import lance_namespace
import requests
from principals import DEX_SCOPE, principal_of


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
DEX = os.environ.get("LANCE_E2E_DEX", "").rstrip("/")
DEX_CLIENT = os.environ.get("LANCE_E2E_OIDC_CLIENT_ID", "lance-catalog")
DEX_SECRET = os.environ.get("LANCE_E2E_DEX_SECRET", "lance-catalog-secret")
ADMIN_USER = os.environ.get("LANCE_E2E_USER", "alice@example.com")
JOB_USER = os.environ.get("LANCE_E2E_PIPELINE_USER", "publisher@rask.internal")


class Identity:
    """One Dex user: its bearer, and the principal the estate's grants name ([[LH-063]])."""

    def __init__(self, email: str) -> None:
        answer = requests.post(
            f"{DEX}/token",
            data={
                "grant_type": "password",
                "client_id": DEX_CLIENT,
                "client_secret": DEX_SECRET,
                "username": email,
                "password": "password",
                "scope": DEX_SCOPE,
            },
            timeout=20,
        )
        answer.raise_for_status()
        self.bearer = str(answer.json()["id_token"])
        self.principal = principal_of(CATALOG, self.bearer)
        self.http = requests.Session()
        self.http.headers["authorization"] = f"Bearer {self.bearer}"

    def namespace(self) -> lance_namespace.LanceNamespace:
        """The stock client a job off the cluster gets."""
        return lance_namespace.connect("rest", {"uri": CATALOG, "headers.Authorization": f"Bearer {self.bearer}"})


def ok(answer: requests.Response) -> dict[str, Any]:
    """The JSON body of a 200 answer; any other status fails, naming the request."""
    assert answer.status_code == 200, f"{answer.request.method} {answer.request.url} -> {answer.status_code}: {answer.text[:500]}"
    return answer.json()


class Tenant:
    """The scratch tenant one run drives, and what it must take down again."""

    def __init__(self, purpose: str, grants: Sequence[str]) -> None:
        self.admin = Identity(ADMIN_USER)
        self.job = Identity(JOB_USER)
        self.project = f"{purpose}-{uuid.uuid4().hex[:8]}"
        self.bronze = f"{self.project}-bronze"
        self.grants = tuple(grants)
        self.tables: list[str] = []

    def create(self) -> None:
        admin = self.admin
        ok(admin.http.post(f"{CATALOG}/management/v1/projects", json={"id": self.project}))
        ok(admin.http.post(f"{CATALOG}/management/v1/warehouses", json={"id": self.project, "project": self.project}))
        body = {"namespace": self.bronze, "properties": {"rask.tier": "bronze"}}
        ok(admin.http.post(f"{CATALOG}/management/v1/warehouses/{self.project}/namespaces", json=body))
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
