"""A client's `delimiter` must not be silently disagreed with (blocker A4).

`delimiter` is a spec query parameter and the reference REST client sends it on EVERY request, taken
from its own configuration. rask declares it on no route — 0 of 153 served operations — and every
handler splits the identifier with the SERVER's `LANCE_NS_DELIMITER` (`$` by default) via
`parse_identifier`. So a client configured with any other delimiter has every multi-segment id
reinterpreted, silently: with `.`, `POST /v1/table/db.t/exists?delimiter=.` is looked up as the single
top-level table `db.t` and answers 404 TableNotFound — a real table reported absent.

**This refuses rather than honours, and that is the smaller half of A4 on purpose.** Honouring the
client's delimiter means threading it through `parse_identifier` AND `fga.canonical_object_id`,
because the FGA object id is derived from the same split — get that wrong and authorization is decided
against a differently-spelled object, which is a worse failure than the one being fixed. Refusing a
mismatch converts a silent wrong answer into a clear one now; the full form is A4 in
`open_lakehouse_diff_left.md`.

The common case is unaffected: the reference client's default IS `$`, so it matches and passes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import DescribeNamespaceResponse


def test_the_servers_own_delimiter_passes(client: TestClient, fake_ns: MagicMock) -> None:
    """What every stock client actually sends."""
    fake_ns.describe_namespace.return_value = DescribeNamespaceResponse()
    assert client.post("/v1/namespace/db/describe?delimiter=%24").status_code == 200


def test_an_absent_delimiter_passes(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_namespace.return_value = DescribeNamespaceResponse()
    assert client.post("/v1/namespace/db/describe").status_code == 200


def test_a_different_delimiter_is_refused_not_misparsed(client: TestClient, fake_ns: MagicMock) -> None:
    fake_ns.describe_namespace.return_value = DescribeNamespaceResponse()
    resp = client.post("/v1/namespace/db/describe?delimiter=.")
    assert resp.status_code == 400, f"a delimiter this server does not use was accepted and the id split with '$' anyway: {resp.status_code}"
    assert "delimiter" in resp.text.lower()
    assert "$" in resp.text, "the refusal must name the delimiter this server DOES use, or the caller cannot fix it"


def test_the_refusal_carries_the_spec_code(client: TestClient, fake_ns: MagicMock) -> None:
    """A code-less 400 is unparseable by the reference client — the same class A5 just closed."""
    fake_ns.describe_namespace.return_value = DescribeNamespaceResponse()
    body = client.post("/v1/namespace/db/describe?delimiter=.").json()
    assert body.get("code") == 13, body  # InvalidInput


@pytest.mark.parametrize("path", ["/v1/table/db$t/exists", "/v1/table/db$t/describe"])
def test_the_refusal_covers_every_v1_route(client: TestClient, fake_ns: MagicMock, path: str) -> None:
    """Router-level, not per route: a per-route check is one the next route added forgets."""
    assert client.post(f"{path}?delimiter=%3A").status_code == 400
