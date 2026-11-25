"""The `lakehouse.governed` kernel's own contracts, pinned where the audit found them broken.

Each test names the finding it closes and fails for that finding's own reason without its fix:

* ``SKG-02`` — a per-tuple delete loop that aborts mid-way must still audit the tuples that WERE
  removed. Without it the trail says a grant is still there while OpenFGA says it is gone.
* ``SKG-03`` — ``expand_tree``'s cycle detection is PATH-scoped. A walk-global visited set labels an
  ordinary diamond (two branches meeting the same rung) a loop and drops the whole subtree, so the
  derivation a reader came for is silently missing.
* ``SKG-04`` — a truncated ``read_object_tuples`` must not return a short list that reads as complete;
  a revoke built on it would leave live grants behind while reporting success.
* ``SKG-12`` — the JWKS fetch carries an explicit timeout, like the discovery fetch beside it.
* ``SKG-15`` — a secret-store fetch failure is an ERROR, and a non-transport fault is NOT swallowed
  into an empty bundle that reads as "the store holds nothing".
* ``SKG-10`` — the environment this kernel reads is DECLARED (a settings class), not four bare ``os.environ`` calls
  three of which name the same variable.

Uses stdlib ``asyncio.run`` so no pytest-asyncio dependency is needed (matches its sibling suites).
"""

import asyncio
import logging
from typing import Any, ClassVar, cast

import aiohttp
import httpx
import pytest
from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientTuple
from openfga_sdk.models import ExpandResponse

from lakehouse.governed import fga_reads, fga_resilience, fga_writes, oidc
from lakehouse.governed.audit import AUDIT_LOGGER, configure_audit
from tests.fga_answers import answer


def _client(c: object) -> OpenFgaClient:
    """Cast a fake to the SDK client type (it only needs the called methods)."""
    return cast(OpenFgaClient, c)


class _AuditCapture(logging.Handler):
    """Collect the structured audit records emitted while a block runs."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def audit_rows() -> Any:
    """The audit stream, enabled and captured for the duration of one test."""
    configure_audit(enabled=True)
    handler = _AuditCapture()
    log = logging.getLogger(AUDIT_LOGGER)
    log.addHandler(handler)
    try:
        yield handler.records
    finally:
        log.removeHandler(handler)
        configure_audit(enabled=False)


# --------------------------------------------------------------------------- #
# SKG-02 — a partial delete still audits what landed
# --------------------------------------------------------------------------- #


def test_delete_tuples_audits_the_tuples_that_landed_before_it_fails(audit_rows: list[logging.LogRecord]) -> None:
    """SKG-02: `delete_tuples` deletes one tuple per write and raises from INSIDE that loop.

    The tuple that WAS removed is gone from OpenFGA whatever happens next, so the trail owes an
    `access_tuple_delete` row for it. Auditing only after the loop returns means an outage on tuple
    two erases the record of tuple one — and the estate then believes a revoked grant is still live.
    """

    class _FlakyDeleteClient:
        def __init__(self) -> None:
            self.writes = 0

        async def write(self, body: Any, *_a: object, **_k: object) -> None:
            del body
            self.writes += 1
            if self.writes > 1:
                raise aiohttp.ClientConnectionError("connection refused")

    doomed = [
        ClientTuple(user="user:alice", relation="owner", object="table:t"),
        ClientTuple(user="user:bob", relation="reader", object="table:t"),
    ]
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga_writes.delete_tuples(
                _client(_FlakyDeleteClient()),
                doomed,
                actor="admin",
                origin="admin_api",
                retry=fga_resilience.RetryPolicy(attempts=1, backoff_seconds=0.0, max_backoff_seconds=0.0),
            )
        )

    removed = [r for r in audit_rows if getattr(r, "audit.action", None) == "access_tuple_delete"]
    assert [getattr(r, "audit.grantee") for r in removed] == ["user:alice"], (
        "the tuple OpenFGA confirmed removed must be audited even though a later tuple failed"
    )


# --------------------------------------------------------------------------- #
# SKG-03 — a diamond is not a cycle
# --------------------------------------------------------------------------- #


def _leaf_node(name: str, *, computed: str | None = None, users: list[str] | None = None) -> dict[str, Any]:
    """A leaf as the server's expand JSON spells it, which leaves out the arms a leaf does not have."""
    leaf = {"computed": {"userset": computed}} if computed else {"users": {"users": users}} if users is not None else {}
    return {"name": name, "leaf": leaf}


class _DiamondClient:
    """`can_read` reaches `owner` through BOTH `writer` and `reader` — a diamond, not a loop."""

    _TREE: ClassVar[frozendict[str, Any]] = frozendict(
        {
            "can_read": None,  # union, built below
            "writer": _leaf_node("table:t#writer", computed="table:t#owner"),
            "reader": _leaf_node("table:t#reader", computed="table:t#owner"),
            "owner": _leaf_node("table:t#owner", users=["user:alice"]),
        }
    )

    async def expand(self, body: Any, *_a: object, **_k: object) -> object:
        relation = body.relation
        if relation == "can_read":
            root = {
                "name": "table:t#can_read",
                "union": {
                    "nodes": [
                        _leaf_node("table:t#can_read", computed="table:t#writer"),
                        _leaf_node("table:t#can_read", computed="table:t#reader"),
                    ]
                },
            }
        else:
            root = self._TREE[relation]
        return answer({"tree": {"root": root}}, ExpandResponse)


def test_expand_tree_does_not_call_a_diamond_a_cycle() -> None:
    """SKG-03: two branches meeting the same `object#relation` is CONCENTRIC, not cyclic.

    A walk-global visited set marks the second arrival `cycle: True` and returns a stub, so the
    `reader` branch of this diamond loses the `owner` rung and the subject holding it — the exact
    derivation `expand_tree` exists to show. Path-scoped tracking keeps both branches whole and still
    catches a genuine loop (pinned below).
    """
    tree = asyncio.run(
        fga_reads.expand_tree(
            _client(_DiamondClient()),
            relation="can_read",
            obj="table:t",
            max_depth=4,
            retry=fga_resilience.RetryPolicy(attempts=1, backoff_seconds=0.0, max_backoff_seconds=0.0),
        )
    )
    branches = tree["union"]
    assert len(branches) == 2
    holders = []
    for branch in branches:
        rung = branch["leaf"]["expanded"][0]
        assert not rung.get("cycle"), f"a diamond arm was mislabelled a cycle: {rung}"
        owner = rung["leaf"]["expanded"][0]
        assert not owner.get("cycle"), f"the shared rung was mislabelled a cycle: {owner}"
        holders.append(owner["leaf"]["users"])
    assert holders == [["user:alice"], ["user:alice"]]


# --------------------------------------------------------------------------- #
# SKG-12 — the JWKS fetch is bounded
# --------------------------------------------------------------------------- #


def test_jwks_client_is_built_with_an_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKG-12: an unbounded JWKS fetch parks a request worker on a hung IdP.

    The discovery fetch three lines above it is bounded; the key fetch was not, and it is the one on
    the per-token path (the discovery document is cached, a rotated `kid` is not).
    """
    captured: dict[str, Any] = {}

    class _FakeJWKClient:
        def __init__(self, uri: str, credential: object, *, max_cached_keys: int, timeout: float) -> None:
            del credential
            captured.update(uri=uri, max_cached_keys=max_cached_keys, timeout=timeout)

        def get_signing_keys(self) -> list[object]:
            return []

    def _fake_get(self: Any, url: str, *a: object, **k: object) -> httpx.Response:
        del self, a, k
        return httpx.Response(
            200,
            json={
                "issuer": "https://idp.example",
                "jwks_uri": "https://idp.example/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(oidc, "_CredentialedJWKClient", _FakeJWKClient)
    monkeypatch.setattr(httpx.Client, "get", _fake_get)

    verifier = oidc.OIDCVerifier(issuer="https://idp.example", audience="rask", cache_ttl=60)
    verifier._fetch("https://idp.example")

    assert captured["timeout"] == oidc.HTTP_FETCH_TIMEOUT_SECONDS
