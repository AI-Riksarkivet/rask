"""Ref writes on one table share its history fence whatever their number, and a reclaim beside one is refused (ADR 0233
§9, ``specs/catalog/Fence.tla``).

Every tag and branch write takes the table's history fence (``lakehouse.history_fences``), so ref writes must never
refuse ref writes. Measured on RustFS 1.0.3 with one shared fence record per table: of 16 concurrent tag writes on one
table 4 answered 409, and of 64, 43, with no reclaim anywhere. Each write here goes through the catalog's own tag door,
on the local root and, in the lane that names an S3 store (``scripts/lifecycle_on_rustfs_check.sh``), on that store,
whose conditional writes and listings arbitrate the fence.
"""

from __future__ import annotations

import threading

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.history_fences import HistoryFencedError, HistoryWork, TableHistory


_TABLE = "lance_catalog$refs$t"
_WRITES = 64


def test_concurrent_ref_writes_all_land_and_a_reclaim_beside_one_is_refused(race_ns_client: TestClient) -> None:
    from catalog.core.config import get_settings

    assert race_ns_client.post("/v1/namespace/lance_catalog$refs/create", json={}).status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64())}))
    created = race_ns_client.post(f"/v1/table/{_TABLE}/create?mode=create", content=rows, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    answers: list[tuple[int, str]] = []
    lock = threading.Lock()
    start = threading.Barrier(_WRITES)

    def _tag(n: int) -> None:
        start.wait()
        answered = race_ns_client.post(f"/v1/table/{_TABLE}/tags/create", json={"tag": f"t{n}", "version": 1})
        with lock:
            answers.append((answered.status_code, answered.text))

    threads = [threading.Thread(target=_tag, args=(n,)) for n in range(_WRITES)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    refused = [text for status, text in answers if status != 200]
    assert not refused, f"{len(refused)} of {_WRITES} tag writes racing only tag writes were refused: {refused[0]}"
    settings = get_settings()
    described = race_ns_client.post(f"/v1/table/{_TABLE}/describe", json={})
    history = TableHistory(
        control_root=settings.registry_root, storage_options=settings.storage_options(), table_uuid=described.json()["properties"]["rask.uuid"]
    )
    history.take(HistoryWork.REFS)
    with pytest.raises(HistoryFencedError):
        history.take(HistoryWork.RECLAIM)
