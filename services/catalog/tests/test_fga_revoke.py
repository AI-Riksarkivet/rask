"""Unit tests for the revoke-on-delete path (lakehouse.governed.fga_writes + catalog.fga_deps).

A dropped object's FGA tuples are removed, so the store holds no grant on an object that is gone. These pin the read+delete contract WITHOUT
the network: the paginated read returns the COMPLETE set, the delete issues exactly those tuples,
an already-absent delete is idempotent, and an OpenFGA outage fails closed (503).

Uses stdlib ``asyncio.run`` so no pytest-asyncio dependency is needed (matches test_fga_resilience).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientTuple
from openfga_sdk.exceptions import ApiException

from catalog.api import fga_deps
from lakehouse.governed import fga_ids, fga_writes


def _tuple(user: str, relation: str, obj: str) -> SimpleNamespace:
    """A read-response tuple: ``.key`` carries user/relation/object (the SDK Tuple shape)."""
    return SimpleNamespace(key=SimpleNamespace(user=user, relation=relation, object=obj))


class _ReadDeleteClient:
    """Fake OpenFGA client: serves canned read pages, records the options each read got, and accumulates
    the per-tuple delete writes (delete_tuples now issues one write per tuple, not a batch)."""

    def __init__(self, pages: list[tuple[list[SimpleNamespace], str]]) -> None:
        self._pages = pages
        self.read_calls = 0
        self.read_options: list[Any] = []
        self.deleted: list[ClientTuple] = []

    async def read(self, _body: Any = None, options: Any = None, *_a: object, **_k: object) -> SimpleNamespace:
        self.read_options.append(options)
        tuples, token = self._pages[self.read_calls]
        self.read_calls += 1
        return SimpleNamespace(tuples=tuples, continuation_token=token)

    async def write(self, body: Any, *_a: object, **_k: object) -> None:
        self.deleted.extend(body.deletes)


def _client(c: object) -> OpenFgaClient:
    """Cast a fake to the SDK client type (it only needs the called methods)."""
    return cast(OpenFgaClient, c)


def _keys(tuples: list[ClientTuple]) -> set[tuple[str, str, str]]:
    return {(t.user, t.relation, t.object) for t in tuples}


class _PartialMissingClient:
    """read returns 3 tuples; the MIDDLE one's delete raises the missing-delete 400 (a concurrent revoke).
    Per-tuple deletes must still remove the OTHER two — a single batched transactional delete would have
    400'd and left ALL three in place (the exact bug the per-tuple split fixes)."""

    def __init__(self) -> None:
        self.deleted: list[ClientTuple] = []

    async def read(self, *_a: object, **_k: object) -> SimpleNamespace:
        return SimpleNamespace(
            tuples=[
                _tuple("user:a", "owner", "table:t"),
                _tuple("user:b", "reader", "table:t"),
                _tuple("user:c", "writer", "table:t"),
            ],
            continuation_token="",
        )

    async def write(self, body: Any, *_a: object, **_k: object) -> None:
        t = body.deletes[0]
        if t.relation == "reader":
            raise _absent_delete_400()
        self.deleted.append(t)


def test_per_tuple_delete_tolerates_one_missing_without_losing_the_rest() -> None:
    fake = _PartialMissingClient()
    removed = asyncio.run(fga_writes.revoke_object_tuples(_client(fake), "table:t", actor="test", origin="create"))
    # The present two are removed and returned; the concurrently-absent one is tolerated — NOT an all-or-nothing loss,
    # and not counted as a removal the server never made.
    assert _keys(fake.deleted) == {("user:a", "owner", "table:t"), ("user:c", "writer", "table:t")}
    assert _keys(removed) == _keys(fake.deleted)


def _settings(*, fga_enabled: bool) -> Any:
    return cast(Any, SimpleNamespace(fga_enabled=fga_enabled))


#: The object a dropped table's grants are held by: its uuid's (ADR 0173 §8).
_TABLE = fga_ids.table_object("0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01")


def test_revoke_ownership_noop_when_fga_disabled() -> None:
    fake = _ReadDeleteClient([([_tuple("user:a", "owner", _TABLE)], "")])
    asyncio.run(fga_deps.revoke_ownership(_client(fake), _settings(fga_enabled=False), obj=_TABLE, token=None, origin="lifecycle_delete"))
    assert fake.read_calls == 0  # FGA off → never touched


def test_a_revoke_with_authorization_on_and_no_client_fails_closed() -> None:
    """A missing client is never read as "authorization is off" ([[LH-585]]): the dropped object would keep its grants
    without a word."""
    with pytest.raises(fga_deps.ServiceUnavailableError, match="authorization service is not available"):
        asyncio.run(fga_deps.revoke_ownership(None, _settings(fga_enabled=True), obj=_TABLE, token=None, origin="lifecycle_delete"))


def test_revoke_ownership_deletes_every_tuple_for_the_object() -> None:
    fake = _ReadDeleteClient([([_tuple("user:a", "owner", _TABLE), _tuple("user:b", "reader", _TABLE)], "")])
    asyncio.run(fga_deps.revoke_ownership(_client(fake), _settings(fga_enabled=True), obj=_TABLE, token=None, origin="lifecycle_delete"))
    assert _keys(fake.deleted) == {
        ("user:a", "owner", _TABLE),
        ("user:b", "reader", _TABLE),
    }


# ── the audit must record what the SERVER confirmed, not what we intended (SKG-01) ──────────────
#
# Re-verified 2026-08-28 (HIGH -> med): the finding's two named failure modes were falsified against
# a live OpenFGA — a write naming an undefined relation/type raises rather than being swallowed, and
# the "does not exist" marker collides with no reachable validation text. What IS real: prose-
# matching a server's error body at all, and the consequence the finding never named — a revoke the
# server says was NEVER THERE was audited as a revoke. OpenFGA does not validate delete tuples
# against the authorization model (probed), so a mis-spelled relation is indistinguishable at the
# API from a concurrent revoke; honest audit is the achievable property, hard rejection is not.


def _absent_delete_400() -> ApiException:
    """The exception the SDK actually raises for a delete of an absent tuple — parsed body included.

    The bare `ApiException(status=400, reason=...)` the older fakes used carries `code=None`, which
    the SDK never produces live: its REST layer parses the JSON body and assigns
    `parsed_exception`, from which `.code` derives. Faking the parse makes the classifier testable
    against the STRUCTURED code rather than the prose this change deletes."""
    from openfga_sdk.models.error_code import ErrorCode
    from openfga_sdk.models.validation_error_message_response import ValidationErrorMessageResponse

    exc = ApiException(status=400, reason="Bad Request")
    exc.parsed_exception = ValidationErrorMessageResponse(
        code=ErrorCode.WRITE_FAILED_DUE_TO_INVALID_INPUT,
        message="cannot delete a tuple which does not exist: ... tuple to be deleted did not exist",
    )
    return exc


class _AbsentDeleteAuditClient:
    """One delete; the server reports the tuple was never there."""

    async def write(self, *_a: object, **_k: object) -> None:
        raise _absent_delete_400()


def test_a_revoke_the_server_says_was_never_there_is_not_audited_as_a_revoke(caplog: pytest.LogCaptureFixture) -> None:
    """SKG-01's real consequence: a compliance query answered "this grant was revoked" for a tuple
    that never existed — a mis-spelled relation in a revoke call left the REAL grant standing while
    the audit trail said it was gone."""
    import logging

    from lakehouse.governed.audit import configure_audit

    configure_audit(enabled=True)
    with caplog.at_level(logging.INFO, logger="lance.audit"):
        asyncio.run(
            fga_writes.delete_tuples(
                _client(_AbsentDeleteAuditClient()),
                [ClientTuple(user="user:alice", relation="parent", object="namespace:n1")],
                actor="admin@example.com",
                origin="grant_api",
            )
        )
    # Extra fields ride the record under an `audit.` prefix (a flat schema for compliance queries),
    # so they are only reachable through getattr with the dotted name.
    claimed = [r for r in caplog.records if getattr(r, "audit.action", "") == "access_tuple_delete" and getattr(r, "audit.reason", None) != "tuple_absent"]
    assert not claimed, "an absent tuple's delete was audited as a confirmed revoke — the trail asserts a removal the server denied performing"
    noop = [r for r in caplog.records if getattr(r, "audit.reason", None) == "tuple_absent"]
    assert noop, "the absence left NO trail at all — an auditor cannot distinguish 'never attempted' from 'attempted, was already gone'"


class _WriteRefusedClient:
    """One Write, answered with ``exc``."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def write(self, *_a: object, **_k: object) -> None:
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ApiException(status=409, reason="Conflict"), id="a-sql-datastore-rolled-back-the-loser"),
        pytest.param(ApiException(status=503, reason="Service Unavailable"), id="an-outage"),
    ],
)
def test_commit_unless_changed_reports_a_rolled_back_write_and_fails_closed_on_an_outage(exc: ApiException) -> None:
    """[[LH-434]] A 409 is a Write a SQL datastore rolled back for another one that overlapped it, so nothing committed and the
    caller reads again; an outage is an unknown outcome and fails closed. The memory datastore never answers 409, so the
    integration test on a real OpenFGA cannot reach this branch."""
    tup = ClientTuple(user="revision:2", relation="admin_revision", object="project:acme")

    async def commit() -> bool:
        return await fga_writes.commit_unless_changed(_client(_WriteRefusedClient(exc)), deletes=[], writes=[tup], actor="a", origin="grant_api")  # noqa: TID251

    if exc.status == 409:
        assert asyncio.run(commit()) is False
    else:
        with pytest.raises(ServiceUnavailableError):
            asyncio.run(commit())
