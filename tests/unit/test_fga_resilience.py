"""Unit tests for the OpenFGA resilience + id-canonicalisation fixes in service_kit.governed.fga.

These pin the audit-confirmed contracts WITHOUT touching the network:

* The dominant outage mode of the aiohttp transport (connection refused / DNS / read
  timeout) is raised as a raw ``aiohttp.ClientError`` / ``TimeoutError`` / ``OSError`` —
  NOT ``openfga_sdk.ApiException``. Those must be classified transient AND fail closed
  (``ServiceUnavailableError`` -> 503), on every read/write path, never escape as a 500.
* The root / delimiter-only id collapses to ``None`` so the grant and check paths agree.

Uses stdlib ``asyncio.run`` so no pytest-asyncio dependency is needed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import aiohttp
import pytest
from lance_namespace import ServiceUnavailableError
from openfga_sdk import ApiException, OpenFgaClient
from openfga_sdk.client.models import ClientCheckRequest, ClientTuple, ClientWriteRequest

from service_kit.governed import fga


def test_is_transient_classifies_network_errors() -> None:
    """A down server (raw transport error) is transient; a definitive non-transport error is not."""
    assert fga._is_transient(aiohttp.ClientConnectionError("refused")) is True
    assert fga._is_transient(TimeoutError()) is True
    assert fga._is_transient(ConnectionRefusedError()) is True  # OSError subclass
    assert fga._is_transient(ValueError("not a transport error")) is False


class _DownClient:
    """Fake OpenFGA client whose every call raises a raw transport error (server down)."""

    async def check(self, *_a: object, **_k: object) -> object:
        raise aiohttp.ClientConnectionError("connection refused")

    async def batch_check(self, *_a: object, **_k: object) -> object:
        raise aiohttp.ClientConnectionError("connection refused")

    async def list_objects(self, *_a: object, **_k: object) -> object:
        raise TimeoutError

    async def list_users(self, *_a: object, **_k: object) -> object:
        raise aiohttp.ClientConnectionError("connection refused")

    async def write(self, *_a: object, **_k: object) -> object:
        raise ConnectionRefusedError


def _down() -> OpenFgaClient:
    """A down server cast to the SDK client type (the fake only needs the called methods)."""
    return cast(OpenFgaClient, _DownClient())


# One attempt + zero backoff keeps the test fast — enough to prove the except path catches
# a non-ApiException transport error and fails closed (rather than escaping as a 500).
def test_check_fails_closed_on_network_error() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.check(
                _down(),
                user="alice",
                relation="can_read_data",
                obj="table:t",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


class _CapturingCheck:
    """Fake client that records the exact subject string sent to OpenFGA Check."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def check(self, request: ClientCheckRequest) -> object:
        self.seen.append(request.user)
        return SimpleNamespace(allowed=True)


def test_check_qualifies_bare_subject() -> None:
    """The common case — callers pass a bare token.sub; fga.check prepends ``user:``."""
    c = _CapturingCheck()
    asyncio.run(fga.check(cast(OpenFgaClient, c), user="alice", relation="can_read_data", obj="table:t"))
    assert c.seen == ["user:alice"]


def test_check_qualify_false_sends_subject_verbatim() -> None:
    """The access simulator (#68) passes a FULL subject: ``qualify=False`` must send it verbatim — never
    double-prefix a user (``user:user:alice`` → always denies) and let a userset be Checked at all
    (audit 2026-07-20 caught the double-prefix bug that made every simulated Check falsely deny)."""
    c = _CapturingCheck()
    asyncio.run(fga.check(cast(OpenFgaClient, c), user="user:alice", relation="can_drop", obj="table:t", qualify=False))
    asyncio.run(
        fga.check(
            cast(OpenFgaClient, c),
            user="role:project_admin#member",
            relation="can_drop",
            obj="table:t",
            qualify=False,
        )
    )
    assert c.seen == ["user:alice", "role:project_admin#member"]


def test_batch_check_fails_closed_on_network_error() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.batch_check(
                _down(),
                user="alice",
                relation="can_write_data",
                objects=["table:t"],
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


def test_list_objects_fails_closed_on_network_error() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.list_objects(
                _down(),
                user="alice",
                relation="can_read_data",
                object_type="table",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


def test_list_users_fails_closed_on_network_error() -> None:
    # CONTRACT (#51): an access review must never answer "nobody" because OpenFGA was down — the
    # outage is a 503, exactly like every other read path.
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.list_users(
                _down(),
                relation="can_read_data",
                obj="table:t",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


def test_list_users_extracts_subjects_and_wildcard() -> None:
    # The wrapper returns sorted bare subjects; a `user:*` public wildcard surfaces as "*" — an
    # access review must never hide a public grant.
    class _Client:
        async def list_users(self, body: object) -> object:
            del body
            return SimpleNamespace(
                users=[
                    SimpleNamespace(object=SimpleNamespace(type="user", id="bob"), wildcard=None),
                    SimpleNamespace(object=SimpleNamespace(type="user", id="alice"), wildcard=None),
                    SimpleNamespace(object=None, wildcard=SimpleNamespace(type="user")),
                ]
            )

    result = asyncio.run(fga.list_users(cast(OpenFgaClient, _Client()), relation="reader", obj="table:t"))
    assert result == ["*", "alice", "bob"]


def test_write_tuples_fails_closed_on_network_error() -> None:
    tuples = [ClientTuple(user="user:alice", relation="owner", object="table:t")]
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.write_tuples(
                _down(),
                tuples,
                actor="test",
                origin="admin_api",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )


class _TransactionalStore:
    """A fake that behaves the way a real OpenFGA store actually does on ``Write``.

    The whole call is ONE transaction. WITHOUT a conflict option, any already-present tuple rejects
    the entire write with ``write_failed_due_to_invalid_input`` and persists NOTHING (verified live
    2026-07-26 — writing [existing, new] left only `existing` behind). WITH
    ``on_duplicate_writes=IGNORE`` (SDK >= 0.10.4, server 1.18.3, verified live 2026-08-28 for
    SKG-01), the same batch answers 200 and lands the non-duplicates in the SAME transaction.
    Modelling BOTH is the point: the old shape is what a server too old for the option still does,
    and the fail-closed test below pins what happens then.
    """

    def __init__(self, existing: set[tuple[str, str, str]], *, honours_conflict: bool = True) -> None:
        self.tuples = set(existing)
        self.honours_conflict = honours_conflict
        self.write_calls: list[int] = []  # batch size per call, so the fast path stays provable

    async def write(self, request: ClientWriteRequest, options: dict[str, object] | None = None) -> object:
        batch = [(t.user, t.relation, t.object) for t in request.writes or []]
        self.write_calls.append(len(batch))
        conflict = (options or {}).get("conflict")
        ignore_duplicates = self.honours_conflict and getattr(conflict, "on_duplicate_writes", None) is not None
        dupes = [t for t in batch if t in self.tuples]
        if dupes and not ignore_duplicates:
            raise ApiException(
                status=400,
                reason=f"cannot write a tuple which already exists: {dupes[0]}: invalid write input",
            )
        self.tuples.update(batch)
        return SimpleNamespace()


def test_write_tuples_lands_siblings_when_one_tuple_already_exists() -> None:
    """A duplicate in the batch must not silently drop the OTHER grants.

    This is the warehouse-owner bug: ``seed_warehouse`` batches [owner on the warehouse, project edge,
    admin on the project] and the e2e seed had already written that last tuple, so OpenFGA rejected the
    whole transaction, ``write_tuples`` swallowed it as "already idempotent", and the creator never got
    ``owner`` — the next call 403'd with ``can_create_namespace required on warehouse:e2e-wh-a``.
    """
    seeded = ("user:alice", "admin", "project:acme")
    store = _TransactionalStore({seeded})
    owner = ClientTuple(user="user:alice", relation="owner", object="warehouse:wh-a")
    parent = ClientTuple(user="project:acme", relation="project", object="warehouse:wh-a")
    dupe = ClientTuple(user="user:alice", relation="admin", object="project:acme")

    asyncio.run(
        fga.write_tuples(
            cast(OpenFgaClient, store),
            [owner, parent, dupe],
            actor="test",
            origin="admin_api",
            retry_attempts=1,
            retry_backoff_seconds=0.0,
            retry_max_backoff_seconds=0.0,
        )
    )

    assert ("user:alice", "owner", "warehouse:wh-a") in store.tuples, "the owner grant was dropped because a SIBLING tuple already existed"
    assert ("project:acme", "project", "warehouse:wh-a") in store.tuples
    # ONE transactional call: the conflict option lands the siblings server-side (SKG-01 replaced
    # the client-side one-tuple-at-a-time fallback, which paid [3, 1, 1, 1] here).
    assert store.write_calls == [3]


def test_a_server_too_old_for_the_conflict_option_fails_CLOSED() -> None:
    """The property the deleted prose-matching fallback actually protected, kept honestly: against
    a server that 400s duplicates regardless of the option, the seam must raise 503 — never guess
    from the message text that the post-condition might hold. A silent success here is exactly the
    warehouse-owner bug (siblings dropped, caller told the grant landed)."""
    store = _TransactionalStore({("user:alice", "owner", "warehouse:wh-a")}, honours_conflict=False)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            fga.write_tuples(
                cast(OpenFgaClient, store),
                [ClientTuple(user="user:alice", relation="owner", object="warehouse:wh-a")],
                actor="test",
                origin="admin_api",
                retry_attempts=1,
                retry_backoff_seconds=0.0,
                retry_max_backoff_seconds=0.0,
            )
        )
    assert store.tuples == {("user:alice", "owner", "warehouse:wh-a")}, "a failed write mutated the store"


def test_write_tuples_batch_without_duplicates_stays_one_call() -> None:
    """No duplicate → a single transactional write, no per-tuple fallback."""
    store = _TransactionalStore(set())
    tuples = [
        ClientTuple(user="user:alice", relation="owner", object="warehouse:wh-b"),
        ClientTuple(user="project:acme", relation="project", object="warehouse:wh-b"),
    ]
    asyncio.run(
        fga.write_tuples(
            cast(OpenFgaClient, store),
            tuples,
            actor="test",
            origin="admin_api",
            retry_attempts=1,
            retry_backoff_seconds=0.0,
            retry_max_backoff_seconds=0.0,
        )
    )
    assert store.write_calls == [2]
    assert len(store.tuples) == 2


def test_write_tuples_single_duplicate_is_still_a_no_op() -> None:
    """The original contract holds for a one-tuple batch: already-granted is success, not an error."""
    seeded = ("user:alice", "owner", "warehouse:wh-a")
    store = _TransactionalStore({seeded})
    asyncio.run(
        fga.write_tuples(
            cast(OpenFgaClient, store),
            [ClientTuple(user="user:alice", relation="owner", object="warehouse:wh-a")],
            actor="test",
            origin="admin_api",
            retry_attempts=1,
            retry_backoff_seconds=0.0,
            retry_max_backoff_seconds=0.0,
        )
    )
    assert store.tuples == {seeded}
    assert store.write_calls == [1]  # one call; the server ignores the duplicate and answers 200


@pytest.mark.parametrize(
    ("table_id", "expected"),
    [
        ("$", None),  # delimiter-only root collapses to None (grant/check agree)
        ("", None),  # empty id
        ("db1", None),  # single top-level segment has no parent namespace
        ("a$b", "a"),  # nested table -> parent namespace
        ("a$b$c", "a$b"),  # two levels deep
    ],
)
def test_parent_namespace_id_root_and_nesting(table_id: str, expected: str | None) -> None:
    assert fga.parent_namespace_id(table_id, delimiter="$") == expected


class _CapturingList:
    """Fake client recording the whole ListObjects request, not only the subject.

    `list_objects` is the allow-list behind every table listing the catalog serves, so what it SENDS is
    the disclosure boundary: the subject decides whose list it is, and the context decides whether a
    time-boxed grant is inside it.
    """

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def list_objects(self, request: Any) -> object:
        self.requests.append(request)
        return SimpleNamespace(objects=["table:acme_gold_catalog"])


def test_list_objects_qualifies_a_bare_subject() -> None:
    """The common case: callers pass the token's bare `sub` and the wrapper prepends `user:`.

    `check` has had this pinned since the 2026-07-20 double-prefix bug; the listing door had the
    identical hatch and no test, and its failure is quieter — a double-prefixed `user:user:alice`
    denies every check loudly, but returns an EMPTY LIST here, which renders as "you have no tables".
    """
    client = _CapturingList()
    asyncio.run(fga.list_objects(cast(OpenFgaClient, client), user="alice", relation="can_get_metadata", object_type="table"))
    assert [r.user for r in client.requests] == ["user:alice"]


def test_list_objects_qualify_false_sends_a_userset_verbatim() -> None:
    """`qualify=False` is what lets the estate-admin explorer ask a USERSET question.

    The wrapper's own docstring states why it must exist: "in THIS model the resource rungs
    deliberately refuse `team#member` directly (a team reaches data through a role), so the userset
    question is the one that explains a real grant". Prefixing it would make that question unaskable.
    """
    client = _CapturingList()
    asyncio.run(
        fga.list_objects(
            cast(OpenFgaClient, client),
            user="role:project_admin#member",
            relation="can_get_metadata",
            object_type="table",
            qualify=False,
        )
    )
    assert [r.user for r in client.requests] == ["role:project_admin#member"]


def test_list_objects_always_sends_a_condition_context() -> None:
    """The clock, and why omitting it is worse on a LISTING than on a check.

    The wrapper's docstring: "a listing that omits the clock silently drops every object reachable only
    through a time-boxed grant, and a short list reads as a correct answer rather than an error." A
    check that loses its context returns a visible `false`; a listing returns a shorter list, and
    nothing about a shorter list looks wrong.
    """
    client = _CapturingList()
    asyncio.run(fga.list_objects(cast(OpenFgaClient, client), user="alice", relation="can_get_metadata", object_type="table"))
    context = client.requests[0].context
    assert context, "ListObjects was sent with no condition context; time-boxed grants vanish silently"
