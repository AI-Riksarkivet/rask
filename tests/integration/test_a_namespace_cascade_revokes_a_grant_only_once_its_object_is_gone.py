"""[[LH-495]] A namespace cascade revokes a grant only once its object is gone, and finishes what a failed deregister left.

`POST /v1/namespace/{id}/drop` with `behavior=Cascade` destroys its subtree bottom-up itself, because the `dir`
backend's drop is Restrict (#117), so it is not atomic. A deregister that fails while its table's row still stands stops
the cascade with the tables before it destroyed and the rest standing; the re-issue that finishes it lists only what
still stands, so a grant or a policy left on a table the first attempt destroyed has nothing left to name it but the
namespace's own `child` edge. A deregister whose answer is lost after the row went leaves a table the cascade can no
longer describe: its bytes are deleted in place ([[LH-532]]), so the first call finishes. Either way grants and policies
are ended only once the tables are gone. A table in the trash is not destroyed by the cascade, and keeps its grants for an
undrop into the namespace created again.

The real catalog app over a real `dir` namespace on pylance, the in-memory `TupleStore` behind the real
`service_kit.governed.fga` seed and revoke, and a backend whose second deregister fails once, before or after the row goes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import DeregisterTableRequest, DeregisterTableResponse, LanceNamespace, ServiceUnavailableError

from catalog.api.dependencies import get_namespace
from catalog.core.config import get_settings
from maintenance.services.purge import objects_held
from service_kit.lakehouse import maintenance_policies
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from tests.integration.openfga_grants import TupleStore, act_as


_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


class _SecondDeregisterFails:
    """The real namespace backend, whose second deregister fails to answer once: before the row goes, or after it."""

    def __init__(self, real: LanceNamespace, *, row_goes: bool) -> None:
        self._real = real
        self._row_goes = row_goes
        self.deregisters = 0

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — every other operation is the real backend's
        return getattr(self._real, name)

    def deregister_table(self, request: DeregisterTableRequest) -> DeregisterTableResponse:
        self.deregisters += 1
        if self.deregisters != 2:
            return self._real.deregister_table(request)
        if self._row_goes:
            self._real.deregister_table(request)
        raise ServiceUnavailableError("the object store did not answer")


@pytest.fixture
def governed(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TupleStore]:
    """The real catalog with OIDC and FGA on and recoverable drops, as alice, over an in-memory tuple store."""
    app = real_ns_client.app
    settings = get_settings().model_copy(
        update={"oidc_enabled": True, "oidc_issuer": "https://idp.example", "oidc_audience": "lance", "fga_enabled": True, "trash_grace_days": 7}
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(app.state, "oidc", MagicMock())
    act_as(real_ns_client, "alice")
    store = TupleStore()
    monkeypatch.setattr(app.state, "fga", store)
    real_ns_client.headers["Authorization"] = "Bearer t"
    yield store


def _ok(client: TestClient, path: str, **kwargs: Any) -> None:  # noqa: ANN401 — the request's own keywords
    answer = client.post(path, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.text}"


def _uuid(client: TestClient, kind: str, name: str) -> str:
    """The uuid the catalog minted for the table or namespace ``name``, which its grants and records are keyed by (ADR 0173 §8)."""
    answer = client.post(f"/v1/{kind}/{name}/describe", json={})
    assert answer.status_code == 200, answer.text
    return str(answer.json()["properties"]["rask.uuid"])


@pytest.mark.parametrize(
    ("row_goes", "deregisters"),
    [
        pytest.param(False, 4, id="a-reissue-finishes-a-cascade-a-failed-deregister-stopped"),
        pytest.param(True, 3, id="a-lost-answer-is-finished-in-place"),
    ],
)
def test_a_finished_cascade_leaves_no_byte_grant_or_policy_of_a_table_it_destroyed(
    real_ns_client: TestClient, governed: TupleStore, row_goes: bool, deregisters: int
) -> None:
    _ok(real_ns_client, "/v1/namespace/db/create", json={})
    tables = ["db$a", "db$b", "db$c"]
    for table in [*tables, "db$t"]:
        _ok(real_ns_client, f"/v1/table/{table}/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    for table in tables:
        _ok(real_ns_client, f"/management/v1/table/{table}/policy/set", json={"retention_days": 7})
    uuids = {table: _uuid(real_ns_client, "table", table) for table in [*tables, "db$t"]}
    locations = {table: str(real_ns_client.post(f"/v1/table/{table}/describe", json={}).json()["location"]) for table in tables}
    assert all(objects_held(location, {}) for location in locations.values()), "a table holds no object, so the delete would prove nothing"
    trashed = f"table:{uuids['db$t']}"
    _ok(real_ns_client, "/v1/table/db$t/drop", json={})
    governed_objects = [f"namespace:{_uuid(real_ns_client, 'namespace', 'db')}", *(f"table:{uuids[table]}" for table in tables)]
    assert len(governed.naming(governed_objects)) == len(governed_objects), "an object was seeded no tuple, so the revoke below would pass vacuously"
    kept = {t for t in governed.tuples if t[2] == trashed}
    assert kept, "the trashed table kept no grant, so the claim that it keeps them would pass vacuously"
    failing = _SecondDeregisterFails(real_ns_client.app.dependency_overrides[get_namespace](), row_goes=row_goes)
    real_ns_client.app.dependency_overrides[get_namespace] = lambda: failing
    cascade = {"params": {"purge": "true"}, "json": {"behavior": "Cascade"}}
    if not row_goes:
        # The attempt the failure stops: the first table is destroyed, and its grants and policy are left for the re-issue.
        stopped = real_ns_client.post("/v1/namespace/db/drop", **cascade)
        assert stopped.status_code == 503, stopped.text

    finished = real_ns_client.post("/v1/namespace/db/drop", **cascade)

    assert finished.status_code == 200, finished.text
    assert failing.deregisters == deregisters, "the failure was never injected where the case puts it, so the case would pass vacuously"
    assert {table: objects_held(location, {}) for table, location in locations.items()} == dict.fromkeys(tables, 0), "a destroyed table left bytes"
    assert {obj: held - kept for obj, held in governed.naming(governed_objects).items() if held - kept} == {}, "a grant outlived the object it was on"
    settings = get_settings()
    left = [table for table in tables if maintenance_policies.get_policy(settings.registry_root, settings.storage_options(), "table", uuids[table]) is not None]
    assert left == [], "a policy outlived the table it was on"
    assert {t for t in governed.tuples if t[2] == trashed} == kept, "a trashed table lost the grants its undrop needs"
