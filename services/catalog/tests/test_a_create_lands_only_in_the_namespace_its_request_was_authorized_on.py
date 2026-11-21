"""A create lands only in the namespace its request was authorized on ([[LH-585]]).

The router authorizes a create on its parent namespace's uuid (ADR 0173 §8, [[LH-524]]), and the backend records the new
object by name. A namespace dropped and created again between the two is another namespace under the same name: bob,
granted `writer` on alice's `lance_catalog$db`, would put a table, or a namespace, into the `lance_catalog$db` alice
created after dropping that one, on which he holds nothing, hanging off a parent no object holds.

So the rival lands exactly there, on each door that brings an object into being under a namespace: alice drops
`lance_catalog$db` and creates it again once the router has authorized bob's request, and before the door writes. The
door answers 409 code 14, the store holds the tuples alice's create wrote and no other, and the name bob sent is free:
alice, who owns the namespace standing there, creates under it.

Every decision is the real model's, on a store of this test's own ([[LH-235]]); every tuple is the one the door writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ConcurrentModificationError, ServiceUnavailableError

from catalog.api.authz import route_guard
from catalog.api.authz.context import AuthzContext
from catalog.api.dependencies import FgaClientDep, ResolverDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core import native
from catalog.services import table_claims
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.records import location_claims


_TABLE = pa.table({"id": pa.array([1, 2], pa.int64())})
_ROWS = encode_arrow_stream(_TABLE)
_NAMESPACE = "/v1/namespace/lance_catalog$db"
#: Where the pipeline's dataset lies for the register door, beneath the namespace root.
_OUTSIDE = "outside"

#: Each door that brings an object into being under a namespace, as bob sends it: (path, keywords).
_DOORS = [
    pytest.param(
        "/v1/table/lance_catalog$db$t/create",
        {"content": _ROWS, "headers": {"Content-Type": ARROW_STREAM_MEDIA_TYPE}},
        id="create",
    ),
    pytest.param("/v1/table/lance_catalog$db$t/declare", {"json": {}}, id="declare"),
    pytest.param("/v1/table/lance_catalog$db$t/register", {"json": {"location": _OUTSIDE}}, id="register"),
    pytest.param(f"{_NAMESPACE}$sub/create", {"json": {}}, id="namespace-create"),
]


class _Raced:
    """Bob's request as the door answered it, with the store's tuples as alice's re-create left them."""

    def __init__(self, answered: Any, as_alice_made_it: set[tuple[str, str, str]]) -> None:  # noqa: ANN401 — the door's httpx response
        self.answered = answered
        self.as_alice_made_it = as_alice_made_it


def _send_raced(catalog: Any, tmp_path: Path, path: str, sent: dict[str, Any]) -> _Raced:  # noqa: ANN401 — the conftest's GovernedCatalog
    """Send bob's create with alice dropping and re-creating ``lance_catalog$db`` once the router has authorized it."""
    # The app module builds its settings at import, so it is imported once the fixture has pinned the environment.
    from catalog.main import app

    alice = catalog.bearer("alice")
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    granted = catalog.client.post("/management/v1/namespace/lance_catalog$db/access/grant", json={"user": "dex~bob", "relation": "writer"}, headers=alice)
    assert granted.status_code == 200, granted.text
    lance.write_dataset(_TABLE, str(tmp_path / _OUTSIDE), data_storage_version="2.2", enable_stable_row_ids=True)
    pending = [True]
    as_alice_made_it: list[set[tuple[str, str, str]]] = []

    def _drop_and_create_again() -> None:
        # One shot, disarmed before it sends anything: alice's own requests are authorized through here too.
        if not pending:
            return
        pending.clear()
        dropped = catalog.client.post(f"{_NAMESPACE}/drop", json={}, headers=alice)
        assert dropped.status_code == 200, dropped.text
        assert catalog.create_in_the_estate("alice", "db").status_code == 200
        as_alice_made_it.append(catalog.store.tuples())

    async def _authorize_then_race(request: Request, settings: SettingsDep, token: CurrentToken, injected: FgaClientDep, resolver: ResolverDep) -> None:
        await route_guard.authorize(request, AuthzContext(client=injected, settings=settings, token=token), resolver=resolver)
        await run_in_threadpool(_drop_and_create_again)

    app.dependency_overrides[route_guard.authorize] = _authorize_then_race
    answered = catalog.client.post(path, headers={**catalog.bearer("bob"), **sent.get("headers", {})}, **_keywords(sent))
    assert as_alice_made_it, f"the rival never landed between bob's authorization and the door: {answered.status_code} {answered.text}"
    return _Raced(answered, as_alice_made_it[0])


def _keywords(sent: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in sent.items() if name != "headers"}


@pytest.mark.parametrize(("path", "sent"), _DOORS)
def test_a_create_raced_by_its_namespaces_drop_and_re_create_lands_nowhere(
    governed_catalog: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    tmp_path: Path,
    path: str,
    sent: dict[str, Any],
) -> None:
    from catalog.core.config import get_settings

    raced = _send_raced(governed_catalog, tmp_path, path, sent)

    answered = raced.answered
    assert answered.status_code == 409, f"the door created in the namespace made under the name after it was authorized: {answered.status_code} {answered.text}"
    assert answered.json().get("code") == ConcurrentModificationError.code, answered.json()
    assert governed_catalog.store.tuples() == raced.as_alice_made_it, "the refused create left a grant behind"
    held = [claim for claim in location_claims.every_claim(table_claims.claim_store(get_settings())) if claim.released_at is None]
    assert held == [], "the refused create left a location held"
    # A read answers an object nobody holds a grant on as absent, so what proves nothing stands is the name being free.
    retried = governed_catalog.client.post(path, headers={**governed_catalog.bearer("alice"), **sent.get("headers", {})}, **_keywords(sent))
    assert retried.status_code == 200, f"an object stands under the name bob sent: {retried.status_code} {retried.text}"


@pytest.mark.parametrize(
    ("path", "sent", "undo", "found", "stands"),
    [
        pytest.param("/v1/table/lance_catalog$db$t/declare", {"json": {}}, "deregister_table", "/v1/table/lance_catalog$db$t", 200, id="declare"),
        pytest.param(
            "/v1/table/lance_catalog$db$t/register", {"json": {"location": _OUTSIDE}}, "deregister_table", "/v1/table/lance_catalog$db$t", 500, id="register"
        ),
        pytest.param(f"{_NAMESPACE}$sub/create", {"json": {}}, "drop_namespace", f"{_NAMESPACE}$sub", 200, id="namespace-create"),
    ],
)
def test_a_raced_create_whose_undo_fails_says_what_stands_and_keeps_its_grants(
    governed_catalog: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    sent: dict[str, Any],
    undo: str,
    found: str,
    stands: int,
) -> None:
    """The store refuses the door's undo, so what it recorded stands where nobody confirmed it belongs. The answer is
    503 naming the remedy, never the 409 a retry would follow into ``AlreadyExists``, and the object keeps the grants
    it was written with, so its creator reads it and can remove it. A registration is refused before its location is
    claimed, so the row left is a table no uuid identifies (code 18), which the estate admin's deregister reaches
    ([[LH-534]])."""
    call = native.call

    def _no_undo(ns: Any, method: str, request: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the backend's own request and answer
        # The rival's own drop is of `db`; the undo's names what bob's request made.
        if method == undo and len(request.id) == 3:
            raise ServiceUnavailableError("the store did not answer the undo")
        return call(ns, method, request, *args, **kwargs)

    monkeypatch.setattr(native, "call", _no_undo)

    raced = _send_raced(governed_catalog, tmp_path, path, sent)

    assert raced.answered.status_code == 503, raced.answered.text
    assert raced.answered.json().get("recorded") is True and raced.answered.json().get("remedy"), raced.answered.json()
    described = governed_catalog.client.post(f"{found}/describe", json={}, headers=governed_catalog.bearer("bob"))
    assert described.status_code == stands, f"what bob's request recorded does not stand as told: {described.status_code} {described.text}"
