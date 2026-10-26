"""[[LH-245]] A commit through a catalog door never deletes a version, armed table or not.

Lance runs version cleanup inside any commit whose resulting manifest carries ``lance.auto_cleanup.*`` config
(`lance_docs/lance/guide/read_and_write.md:610-671`), under whoever commits and past every legal hold and protected base.
Measured live on helm rev 280 before this fix: three appends through the insert door on an armed table
deleted versions 1 and 2, because the sweep that disarms a table had not reached it. So the door that
commits disarms the ref first, and the append rebases onto that config-only commit and deletes nothing
(measured on pylance 12.0.0).

The table is armed out of band, straight through pylance, the way a maintain-tier credential can: no catalog
door writes manifest config, and the property doors refuse the keys. Driven through the real catalog app on
a real pylance ``dir`` backend, through the append door ``/insert`` (the catalog writes the rows) and the erasure door,
which disarms every ref so that the bring-your-own job's commits are admitted at ``version/create`` ([[LH-493]]) and
commits nothing else.
"""

from __future__ import annotations

import io
from typing import Literal

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "lh245$t"
_SCHEMA = pa.schema([("id", pa.int64())])
#: What an armed table carries: ``interval`` arms the hook, and ``older_than=0s`` with ``retain_versions=1``
#: lets one commit delete every older version (measured on 12.0.0).
_ARMED: dict[str, str | None] = {"lance.auto_cleanup.interval": "1", "lance.auto_cleanup.older_than": "0s", "lance.auto_cleanup.retain_versions": "1"}


def _rows(*ids: int) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64())}, schema=_SCHEMA)


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _append(client: TestClient, marker: int) -> None:
    response = client.post(f"/v1/table/{TABLE}/insert", content=_ipc(_rows(marker)), headers=ARROW)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("door", ["insert", "erasure"])
def test_door_commits_on_an_armed_table_keep_every_version(real_ns_client: TestClient, door: Literal["insert", "erasure"]) -> None:
    assert real_ns_client.post("/v1/namespace/lh245/create", json={}).status_code == 200
    created = real_ns_client.post(f"/v1/table/{TABLE}/create", content=_ipc(_rows(1)), headers=ARROW)
    assert created.status_code == 200, created.text
    location = str(created.json()["location"])
    _append(real_ns_client, 2)
    lance.dataset(location).update_config(_ARMED)
    kept = {v["version"] for v in lance.dataset(location).versions()}

    if door == "erasure":
        erased = real_ns_client.post(f"/management/v1/table/{TABLE}/erasure", json={"predicate": "id = 2", "retain_days": 30})
        assert erased.status_code == 202, erased.text
    else:
        for marker in (10, 11, 12):
            _append(real_ns_client, marker)

    after = lance.dataset(location)
    assert kept <= {v["version"] for v in after.versions()}, (
        f"a commit through /{door} deleted versions {sorted(kept - {v['version'] for v in after.versions()})}"
    )
    assert not [key for key in after.config() if key.startswith("lance.auto_cleanup.")], f"/{door} left the table armed: {after.config()}"
    assert sorted(after.to_table()["id"].to_pylist()) == ([1, 2] if door == "erasure" else [1, 2, 10, 11, 12])
