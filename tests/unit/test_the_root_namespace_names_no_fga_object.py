"""The spec's ROOT namespace id must not compose an empty OpenFGA object.

lance-ns encodes the root namespace as the delimiter alone. The stock client's
`list_namespaces(ListNamespacesRequest(id=[]))` therefore issues

    GET /v1/namespace/%24/list?delimiter=%24

and `parse_identifier("$", "$")` returns ZERO segments — so `_object` composes `namespace:` with an
empty id. OpenFGA refuses that outright:

    openfga_sdk.exceptions.ValidationException:
    [check] HTTP 400 invalid relation: invalid 'object' field format (validation_error)

which leaves `authorize` as a 500 on a spec-legal request. No tuple can name that object either, so
the check could never have succeeded — it is not a permission question, it is a malformed one.

MEASURED 2026-09-24 by driving the STOCK `lance_namespace` client against a governed catalog:
`list_namespaces` answered `ServiceUnavailableError: Internal Server Error`. It passes against the
deployed estate only because warehouses are enabled there, so nothing exercised this path — and the
conformance suite that would have caught it runs in no lane.

SKIPPING THE CHECK OPENS NOTHING. `list_namespaces` filters every NAME it returns through
`fga.list_objects` on `can_get_metadata` — the route's documented design is that the route opens and
the ITEMS are checked, because narrowing the route to 403 breaks the breadcrumb a grantee needs to
reach their own table. Authentication is enforced before any of this.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from lance_namespace import InvalidInputError

from catalog.api import fga_deps


class _Token:
    sub = "user:alice"


class _Settings:
    fga_enabled = True
    delimiter = "$"


class _Request:
    def __init__(self, path: str, object_id: str) -> None:
        self.scope = {"path": path}
        self.path_params = {"id": object_id}


def _drive(monkeypatch: pytest.MonkeyPatch, path: str, object_id: str) -> list[str]:
    """Run the guard, returning every object it asked OpenFGA about."""
    asked: list[str] = []

    async def _record_check(_client: object, *, user: str, relation: str, obj: str, **_: Any) -> bool:
        del user, relation
        asked.append(obj)
        return True

    async def _record_batch(_client: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        del user, relation
        asked.extend(objects)
        return dict.fromkeys(objects, True)

    monkeypatch.setattr(fga_deps.fga, "check", _record_check)
    monkeypatch.setattr(fga_deps.fga, "batch_check", _record_batch)
    # CAST, the idiom `test_the_bus_door_authorizes_what_it_records` uses: the stand-ins carry exactly
    # the attributes the guard reads, and constructing a real Request/Settings/IDToken/OpenFgaClient
    # would drag a whole app and a live store into a test about one composed object id.
    asyncio.run(fga_deps.authorize(cast(Any, _Request(path, object_id)), cast(Any, _Settings()), cast(Any, _Token()), cast(Any, object())))
    return asked


def test_listing_the_root_asks_openfga_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact request the stock client makes."""
    asked = _drive(monkeypatch, "/v1/namespace/$/list", "$")
    assert asked == [], f"the guard checked {asked} for the root namespace — `namespace:` is not an object OpenFGA accepts"


def test_a_named_namespace_is_still_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A control: if the guard stopped checking named namespaces, the test above would pass for the
    wrong reason and the route would be open to everyone."""
    asked = _drive(monkeypatch, "/v1/namespace/acme-bronze/list", "acme-bronze")
    assert asked == ["namespace:acme-bronze"], f"a named namespace must still be authorized, got {asked}"


def test_an_empty_id_on_a_table_is_a_typed_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A table id with no segments is malformed, not a root — so it earns the spec's own 400 rather
    than an empty object or a silent open."""
    with pytest.raises(InvalidInputError):
        _drive(monkeypatch, "/v1/table/$/describe", "$")
