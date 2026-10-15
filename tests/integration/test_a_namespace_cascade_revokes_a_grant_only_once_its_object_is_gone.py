"""[[LH-495]] A namespace cascade revokes a grant only once its object is gone, and a re-issue finishes what a failure left.

`POST /v1/namespace/{id}/drop` with `behavior=Cascade` destroys its subtree bottom-up itself, because the `dir`
backend's drop is Restrict (#117), so it is not atomic: a failure part-way leaves some tables destroyed and the rest
standing. The re-issue that finishes it lists only what still stands, so a grant or a policy left on a table the first
attempt destroyed has nothing left to name it but the namespace's own `child` edge. A table in the trash is not
destroyed by the cascade, and keeps its grants for an undrop into the namespace created again.

The real catalog app over a real `dir` namespace on pylance, the in-memory `TupleStore` behind the real
`service_kit.governed.fga` seed and revoke, and a backend that drops the second table it is asked to and then fails to
answer, once: the table is gone before the door can end anything it held.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import DropTableRequest, DropTableResponse, LanceNamespace, ServiceUnavailableError

from catalog.api.dependencies import get_namespace
from catalog.core.config import get_settings
from service_kit.lakehouse import maintenance_policies
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from tests.integration.openfga_grants import TupleStore, act_as


_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


class _SecondDropUnanswered:
    """The real namespace backend, which drops the second table it is asked to and then fails to answer, once."""

    def __init__(self, real: LanceNamespace) -> None:
        self._real = real
        self.drops = 0

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — every other operation is the real backend's
        return getattr(self._real, name)

    def drop_table(self, request: DropTableRequest) -> DropTableResponse:
        self.drops += 1
        dropped = self._real.drop_table(request)
        if self.drops == 2:
            raise ServiceUnavailableError("the object store did not answer")
        return dropped


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


def test_a_cascade_that_fails_part_way_leaves_no_grant_or_policy_once_a_reissue_finishes_it(real_ns_client: TestClient, governed: TupleStore) -> None:
    _ok(real_ns_client, "/v1/namespace/db/create", json={})
    tables = ["db$a", "db$b", "db$c"]
    for table in [*tables, "db$t"]:
        _ok(real_ns_client, f"/v1/table/{table}/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    for table in tables:
        _ok(real_ns_client, f"/management/v1/table/{table}/policy/set", json={"retention_days": 7})
    _ok(real_ns_client, "/v1/table/db$t/drop", json={})
    governed_objects = ["namespace:db", *(f"table:{table}" for table in tables)]
    assert len(governed.naming(governed_objects)) == len(governed_objects), "an object was seeded no tuple, so the revoke below would pass vacuously"
    kept = {t for t in governed.tuples if t[2] == "table:db$t"}
    assert kept, "the trashed table kept no grant, so the claim that it keeps them would pass vacuously"
    failing = _SecondDropUnanswered(real_ns_client.app.dependency_overrides[get_namespace]())
    real_ns_client.app.dependency_overrides[get_namespace] = lambda: failing
    cascade = {"params": {"purge": "true"}, "json": {"behavior": "Cascade"}}
    first = real_ns_client.post("/v1/namespace/db/drop", **cascade)
    assert first.status_code == 503, first.text

    finished = real_ns_client.post("/v1/namespace/db/drop", **cascade)

    assert finished.status_code == 200, finished.text
    assert {obj: held - kept for obj, held in governed.naming(governed_objects).items() if held - kept} == {}, "a grant outlived the object it was on"
    settings = get_settings()
    left = [table for table in tables if maintenance_policies.get_policy(settings.registry_root, settings.storage_options(), "table", table) is not None]
    assert left == [], "a policy outlived the table it was on"
    assert {t for t in governed.tuples if t[2] == "table:db$t"} == kept, "a trashed table lost the grants its undrop needs"
