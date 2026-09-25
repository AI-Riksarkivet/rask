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

MEASURED 2026-09-24 on BOTH estates, and they did not agree — which is the sharper reason this
needs fixing rather than tolerating. Same catalog code, same `openfga/openfga:v1.18.3`:

  * hermetic store — `check` on `namespace:` is REFUSED `400 invalid 'object' field format`, the
    exception escapes `authorize`, and the stock client sees
    `ServiceUnavailableError: Internal Server Error`.
  * deployed store — the same `check` is ACCEPTED and answered from tuples. The live audit line reads
    `audit.action='can_get_metadata' audit.outcome='allow' audit.resource='namespace:'`, and asking
    that store directly for a subject with no grants returns `allowed: false`.

So one estate 500s and the other renders a permission verdict about an object that cannot exist. It
is not fail-open — the ungranted subject is refused — but a decision keyed on an empty id is not a
decision, and which of the two an operator meets depends on their store rather than on their code.
The conformance suite that drives this path runs in no lane, so neither behaviour was ever seen.

SKIPPING THE CHECK IS SAFE ONLY ON THE READS THAT FILTER EVERY ITEM. `list` and `table/list` filter
each name they return through FGA, and `describe`/`exists` of the root reveal nothing a tuple guards.
Every other action on the root (drop, policy, protection, grants) is refused: an empty id exempted from
the check lets any signed-in caller cascade-drop the whole default root.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import InvalidInputError, PermissionDeniedError

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


@pytest.mark.parametrize("suffix", ["list", "table/list", "describe", "exists"])
def test_the_root_reads_that_filter_every_item_stay_open(monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    assert _drive(monkeypatch, f"/v1/namespace/$/{suffix}", "$") == []


@pytest.mark.parametrize("suffix", ["drop", "policy/set", "policy/delete", "protection", "access/grant", "access/revoke", "access/list", "undrop"])
def test_every_other_action_on_the_root_is_refused(monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    """No tuple can name the root, so an action that is not a per-item-filtered read cannot be authorized on it."""
    with pytest.raises(PermissionDeniedError):
        _drive(monkeypatch, f"/v1/namespace/$/{suffix}", "$")


@pytest.mark.parametrize("purge", [False, True])
def test_a_cascade_drop_of_the_root_is_refused_before_any_table_is_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, purge: bool) -> None:
    """The door refuses it too, so the root survives a deployment that runs with FGA off."""
    from catalog.core.config import get_settings

    root = tmp_path / "lance-catalog"
    root.mkdir()
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"file://{root}",
        "LANCE_CONTROL_ROOT": f"file://{tmp_path / 'control'}",
        "LANCE_TRASH_GRACE_DAYS": "1",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_S3_ACCESS_KEY_ID": "x",
        "LANCE_S3_SECRET_ACCESS_KEY": "x",
        "LANCE_S3_ENDPOINT_URL": "http://127.0.0.1:9",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from catalog.main import app

    lance.write_dataset(pa.table({"id": pa.array([1, 2], pa.int64())}), str(root / "t"), data_storage_version="2.2", enable_stable_row_ids=True)
    try:
        with TestClient(app) as client:
            assert client.post("/v1/namespace/db/create", json={}).status_code == 200
            assert client.post("/v1/table/db$t/register", json={"location": "t"}).status_code == 200

            resp = client.post(f"/v1/namespace/$/drop{'?purge=true' if purge else ''}", json={"behavior": "Cascade"})

            assert resp.status_code == 400, resp.text
            assert client.post("/v1/table/db$t/exists").status_code == 200, "a refused root drop must leave every table registered"
            assert lance.dataset(str(root / "t")).count_rows() == 2
    finally:
        get_settings.cache_clear()
