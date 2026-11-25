"""The spec's ROOT namespace id must not compose an empty OpenFGA object.

lance-ns encodes the root namespace as the delimiter alone. The stock client's
`list_namespaces(ListNamespacesRequest(id=[]))` therefore issues

    GET /v1/namespace/%24/list?delimiter=%24

and `parse_identifier("$", "$")` returns ZERO segments — so `addressed_object` composes `namespace:` with an
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

import asyncio
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from lance_namespace import InvalidInputError, PermissionDeniedError

from catalog.api.authz import route_guard
from catalog.api.authz.context import AuthzContext
from lakehouse.governed import fga_provision
from tests.callers import person
from tests.catalog_settings import governed_settings
from tests.unit.named_resolver import NamedResolver, uuid_of


_CALLER = person("alice")


def _request(path: str, object_id: str) -> Request:
    """The request the router hands the guard: its path, the id it matched, and the method."""
    return Request({"type": "http", "method": "POST", "path": path, "path_params": {"id": object_id}, "headers": []})


def _drive(path: str, object_id: str) -> None:
    """Run the guard on a bare-object client: a question it put to OpenFGA would fail with the client's
    ``AttributeError``, so the refusal each caller expects is one made before OpenFGA is asked."""
    asyncio.run(
        route_guard.authorize(
            _request(path, object_id),
            AuthzContext(client=cast(Any, object()), settings=governed_settings(), token=_CALLER),
            resolver=NamedResolver(),
        )
    )


@pytest.mark.parametrize("held", [pytest.param(None, id="a-caller-holding-nothing-is-refused"), pytest.param("reader", id="a-reader-is-admitted")])
def test_a_named_namespace_is_still_checked(openfga_store: Any, held: str | None) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """If the guard stopped checking named namespaces, the route would be open to everyone: on the real model a caller
    holding nothing on ``acme-bronze`` is refused its listing, and its reader is admitted."""
    if held is not None:
        openfga_store.write(("user:dex~alice", held, f"namespace:{uuid_of('namespace', ['acme-bronze'])}"))
    request = _request("/v1/namespace/acme-bronze/list", "acme-bronze")

    async def _authorized() -> None:
        async with fga_provision.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            await route_guard.authorize(request, AuthzContext(client=client, settings=governed_settings(), token=_CALLER), resolver=NamedResolver())

    with pytest.raises(PermissionDeniedError, match=r"^can_get_metadata required on namespace:acme-bronze$") if held is None else nullcontext():
        asyncio.run(_authorized())


def test_an_empty_id_on_a_table_is_a_typed_refusal() -> None:
    """A table id with no segments is malformed, not a root — so it earns the spec's own 400 rather
    than an empty object or a silent open."""
    with pytest.raises(InvalidInputError, match=r"^table id is empty: '\$' names no table$"):
        _drive("/v1/table/$/describe", "$")


@pytest.mark.parametrize("suffix", ["drop"])
def test_every_other_action_on_the_root_is_refused(suffix: str) -> None:
    """No tuple can name the root, so an action that is not a per-item-filtered read cannot be authorized on it."""
    with pytest.raises(PermissionDeniedError, match=rf"^the root namespace names no object, so '{suffix}' cannot be authorized on it$"):
        _drive(f"/v1/namespace/$/{suffix}", "$")


def test_a_cascade_drop_of_the_root_is_refused_before_any_table_is_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            assert client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
            assert client.post("/v1/table/lance_catalog$db$t/register", json={"location": "t"}).status_code == 200

            resp = client.post("/v1/namespace/$/drop?purge=true", json={"behavior": "Cascade"})

            assert resp.status_code == 400, resp.text
            assert client.post("/v1/table/lance_catalog$db$t/exists").status_code == 200, "a refused root drop must leave every table registered"
            assert lance.dataset(str(root / "t")).count_rows() == 2
    finally:
        get_settings.cache_clear()
