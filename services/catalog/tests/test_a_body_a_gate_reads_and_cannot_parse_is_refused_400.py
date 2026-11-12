"""A body the authorization gate reads to decide and cannot parse is the caller's malformed request: 400, never a 500.

The grant doors' gate reads the rung being granted from the body, and the batch doors' gate the tables each operation
names, both before the route's own model reads it, because the path does not say what is being authorized. A body that
is not JSON, read unguarded there, raises past every handler and answers 500 code 18, a server fault, for what is the
caller's malformed request. FastAPI parses a body itself only when it is declared JSON (fastapi 0.140.13 answers that
one 422 before any gate runs), so the gate's read is the first a body of another type meets. Driven against the catalog
with authorization on: a loopback Dex signs alice in and a real OpenFGA decides on a store of the test's own.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.parametrize(
    "door",
    [
        pytest.param("/management/v1/namespace/bronze/access/grant", id="a-grant"),
        pytest.param("/v1/table/batch-commit", id="a-batch"),
    ],
)
def test_a_body_the_gate_cannot_parse_is_refused_400(governed_catalog: Any, door: str) -> None:  # noqa: ANN401 — the catalog conftest's GovernedCatalog
    alice = governed_catalog.bearer("alice")
    assert governed_catalog.client.post("/v1/namespace/bronze/create", json={}, headers=alice).status_code == 200

    refused = governed_catalog.client.post(door, content=b"{", headers={**alice, "Content-Type": "text/plain"})

    assert (refused.status_code, refused.headers["content-type"], refused.json()["code"]) == (400, "application/problem+json", 13), refused.text
