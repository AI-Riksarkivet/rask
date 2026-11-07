"""A check-only service asks the catalog's decision door and is answered only inside the scope it is granted ([[LH-442]], [[LH-563]]).

OpenFGA authorizes none of its callers, so a service holding an OpenFGA token can write any tuple. The services that
only check hold none and ask the catalog: `lakehouse.governed.decider.CatalogDecider`, presenting the pod's projected
`rask-catalog` token, against the real catalog app on a real socket, every decision the real model's on an OpenFGA store
of the test's own.

A service's scope is a grant to its own identity. Lineage serves every tenant and holds `decider` and `governance_prober`
on the estate root, as the chart's bootstrap hook writes them. A service serving one tenant holds `decider` on that
tenant's project alone, granted by the project's admin at the project's access door, and is answered about the
project's tables: another project's table is answered refused, exactly as a table that does not exist is, so the service
learns nothing outside its scope. A notifier holds `member_expander` on the estate root, which the estate admin grants at
the tuple door. What may be asked is closed: a role such as `writer` is not a question a decision answers, and neither is
a decision rung. The door refuses a route its caller holds no rung for 403 (code 15) and a question it does not answer
400 (code 13), and every refusal reaches the service as `ServiceUnavailableError`, its gates' 503.

Arranged through the doors on a `dir` catalog with warehouses on: two projects, each holding one warehouse, whose record
and project edge are written where the warehouse door writes them (the door provisions a bucket), a namespace bound to
it through the warehouse's namespace door and one table.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pytest
import uvicorn
from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, ConfigDict

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed.decider import CatalogDecider, TableDecision
from lakehouse.governed.fga import user_object


#: The two tenants, each mapped to its warehouse and the namespace bound to it; each namespace holds one table, `<ns>$t`.
ACME, BETA = "acme", "beta"
_TENANTS = {ACME: ("wh-acme", "lake"), BETA: ("wh-beta", "far")}
LAKE, FAR = "lake$t", "far$t"
#: The accounts that ask: lineage, a service of acme's and a notifier.
LINEAGE, ACME_SERVICE, NOTIFIER = "rask-sa-lineage", "rask-sa-acme-service", "rask-sa-a-notifier"
#: A table uuid no table holds and no tuple names.
_STRAY = "table:00000000-0000-4000-8000-000000000000"


def _account(sa: str) -> str:
    """The principal the catalog derives for ``sa``'s projected token."""
    return f"kubernetes~system:serviceaccount:default:{sa}"


def _ok(answer: httpx.Response) -> httpx.Response:
    assert answer.status_code in (200, 201), f"{answer.request.method} {answer.request.url}: {answer.status_code} {answer.text}"
    return answer


class Estate(BaseModel):
    """The catalog's address, and each table's name mapped to its uuid and its namespace's."""

    model_config = ConfigDict(frozen=True)

    url: str
    uuids: dict[str, tuple[str, str]]

    def asked(self, name: str) -> str:
        """The object a question names ``name`` by: a table's uuid object, or ``name`` itself for any other object."""
        return f"table:{self.uuids[name][0]}" if name in self.uuids else name

    def decision(self, name: str, *, allowed: bool) -> TableDecision:
        """The door's answer about the table ``name`` names: with its uuids only when allowed."""
        if not allowed:
            return TableDecision(allowed=False)
        table, namespace = self.uuids[name]
        return TableDecision(allowed=True, table_uuid=table, namespace_uuid=namespace)


class Refused(BaseModel):
    """The door's refusal of a question: the status it answers and the spec's code in its problem body."""

    model_config = ConfigDict(frozen=True)

    status: int
    code: int


#: A route its caller holds no rung for, and a question the door does not answer.
_FORBIDDEN, _MALFORMED = Refused(status=403, code=15), Refused(status=400, code=13)


def _arrange(url: str, store: Any, dex_issuer: Any) -> dict[str, tuple[str, str]]:  # noqa: ANN401 — the root conftest's OpenFgaStore and DexIssuer
    """root, the estate admin as the bootstrap hook grants it, makes both tenants through the doors and grants bob reading
    both tables and acme's membership; acme's admin grants acme's service the asking on acme, and the estate admin grants
    a notifier the expansion of members at the tuple door."""
    from catalog.core.config import get_settings
    from catalog.services import warehouses

    store.write(
        (user_object("dex~root"), "admin", "estate:rask"),
        *((user_object(_account(LINEAGE)), role, "estate:rask") for role in ("decider", "governance_prober")),
    )
    settings, rows = get_settings(), encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))
    root = {"Authorization": f"Bearer {dex_issuer.mint('root', connector='local', audience='lance-catalog')}"}
    uuids: dict[str, tuple[str, str]] = {}
    with httpx.Client(base_url=url, headers=root, timeout=30) as client:
        for project, (warehouse, namespace) in _TENANTS.items():
            _ok(client.post("/management/v1/projects", json={"id": project}))
            made = {
                "id": warehouse,
                "bucket": warehouse,
                "root_uri": settings.root,
                "project": project,
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
            }
            warehouses.create_warehouse_record(settings.registry_root, settings.storage_options(), made)
            store.write((f"project:{project}", "project", f"warehouse:{warehouse}"))
            _ok(client.post(f"/management/v1/warehouses/{warehouse}/namespaces", json={"namespace": namespace}))
            table = f"{namespace}$t"
            _ok(client.post(f"/v1/table/{table}/create", content=rows, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE}))
            _ok(client.post(f"/management/v1/table/{table}/access/grant", json={"user": "dex~bob", "relation": "reader"}))
            described = _ok(client.post(f"/v1/table/{table}/describe", json={})).json()["properties"]
            enclosing = _ok(client.post(f"/v1/namespace/{namespace}/describe", json={})).json()["properties"]
            uuids[table] = (described["rask.uuid"], enclosing["rask.uuid"])
        _ok(client.put(f"/management/v1/projects/{ACME}/members", json={"user": "dex~bob", "relation": "member"}))
        _ok(client.post("/management/v1/access/tuples", json={"user": user_object(_account(NOTIFIER)), "relation": "member_expander", "object": "estate:rask"}))
        _ok(client.post(f"/management/v1/projects/{ACME}/access/grant", json={"user": _account(ACME_SERVICE), "relation": "decider"}))
    return uuids


@pytest.fixture
def estate(sa_issuer: Any, dex_issuer: Any, openfga_store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Estate]:  # noqa: ANN401 — the root conftest's fixtures
    """The catalog on a live socket, verifying people's and service accounts' tokens, deciding on the test's OpenFGA store."""
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(tmp_path),
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_WAREHOUSES_ENABLED": "true",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": dex_issuer.issuer,
        "RASK_OIDC_AUDIENCE": "lance-catalog",
        "RASK_OIDC_ALLOW_INSECURE": "true",
        "RASK_OIDC_CONNECTORS": '{"local": "dex"}',
        "RASK_SA_ISSUER": sa_issuer.issuer,
        "RASK_SA_AUDIENCE": "rask-catalog",
        "RASK_SA_FETCH_TOKEN_FILE": str(sa_issuer.fetch_token_file),
        "RASK_SA_CA_FILE": str(sa_issuer.ca_file),
        "RASK_FGA_ENABLED": "true",
        "RASK_FGA_API_URL": openfga_store.url,
        "RASK_FGA_STORE_ID": openfga_store.store_id,
        "RASK_FGA_MODEL_ID": openfga_store.model_id,
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    url = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    try:
        yield Estate(url=url, uuids=_arrange(url, openfga_store, dex_issuer))
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        get_settings.cache_clear()


#: (the account asking, the question, its answer by what was asked, or the door's refusal). A table is asked about by
#: its name in `tables` and by its uuid object in `objects` and `governed`.
_CASES = [
    pytest.param(LINEAGE, ("tables", "can_read_data", [LAKE, FAR]), {LAKE: True, FAR: True}, id="an-estate-decider-decides-in-every-tenant"),
    pytest.param(ACME_SERVICE, ("tables", "can_read_data", [LAKE, FAR]), {LAKE: True, FAR: False}, id="another-tenants-table-is-answered-as-absent"),
    pytest.param(ACME_SERVICE, ("objects", "can_read_data", [LAKE, FAR]), {LAKE: True, FAR: False}, id="and-so-is-its-uuid"),
    pytest.param(LINEAGE, ("governed", [LAKE, _STRAY]), {LAKE: True, _STRAY: False}, id="a-governance-prober-probes"),
    pytest.param(ACME_SERVICE, ("governed", [LAKE]), _FORBIDDEN, id="a-tenants-decider-may-not-probe"),
    pytest.param(NOTIFIER, ("list-users", "member", f"project:{ACME}"), ["dex~bob", "dex~root"], id="a-member-expander-expands"),
    pytest.param(LINEAGE, ("list-users", "member", f"project:{ACME}"), _FORBIDDEN, id="lineage-may-not-expand-members"),
    pytest.param(NOTIFIER, ("list-users", "admin", f"project:{ACME}"), _MALFORMED, id="only-a-membership-userset-expands"),
    pytest.param(
        LINEAGE,
        ("objects", "member", [f"project:{ACME}", f"project:{BETA}"]),
        {f"project:{ACME}": True, f"project:{BETA}": False},
        id="a-membership-is-a-question",
    ),
    pytest.param(LINEAGE, ("objects", "writer", [LAKE]), _MALFORMED, id="a-role-is-not-a-question"),
    pytest.param(LINEAGE, ("objects", "can_decide_access", ["estate:rask"]), _MALFORMED, id="the-decision-rungs-are-not-a-question"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("account", "question", "answer"), _CASES)
async def test_a_check_only_service_is_answered_only_inside_its_scope(
    estate: Estate,
    sa_issuer: Any,  # noqa: ANN401 — the root conftest's issuer
    caplog: pytest.LogCaptureFixture,
    account: str,
    question: tuple[Any, ...],
    answer: dict[str, bool] | list[str] | Refused,
) -> None:
    token = sa_issuer.mint(account, audience="rask-catalog")
    decider = CatalogDecider(estate.url, token=lambda: token, timeout_seconds=10)
    route, *args = question

    async def ask() -> dict[str, TableDecision] | dict[str, bool] | list[str]:
        if route == "tables":
            return await decider.check_tables(user="dex~bob", relation=args[0], tables=args[1])
        if route == "objects":
            return await decider.batch_check(user="dex~bob", relation=args[0], objects=[estate.asked(name) for name in args[1]])
        if route == "governed":
            return await decider.governed([estate.asked(name) for name in args[0]])
        return await decider.list_users(relation=args[0], obj=args[1])

    try:
        if isinstance(answer, Refused):
            with caplog.at_level(logging.ERROR, logger="lakehouse.governed.decider"), pytest.raises(ServiceUnavailableError):
                await ask()
            # The client logs the door's answer as it fails closed: the status, and the problem body it read.
            told = [vars(r) for r in caplog.records if r.name == "lakehouse.governed.decider" and r.getMessage() in ("decision_refused", "decision_failed")]
            assert [Refused(status=r["status"], code=json.loads(r["body"])["code"]) for r in told] == [answer]
        elif route == "tables":
            assert isinstance(answer, dict)
            assert await ask() == {name: estate.decision(name, allowed=allowed) for name, allowed in answer.items()}
        elif isinstance(answer, dict):
            assert await ask() == {estate.asked(name): allowed for name, allowed in answer.items()}
        else:
            assert await ask() == answer
    finally:
        await decider.close()
