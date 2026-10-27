"""A restore commits the restored version's manifest config again, so the restore door refuses a version carrying config
no writer may set ([[LH-245]], [[LH-538]]).

`lance.auto_cleanup.*` deletes versions inside the commit that carries it, and `lance.compaction.*` plans a compaction
in place of the table's maintenance policy (`lance_docs/lance/format/table/table_config.md`). `version/create` and every
property door refuse both, so a version carrying them is one written before that refusal or attached by a writer at the
store; restoring it would commit them again. Driven through the real catalog app on a real `dir` namespace, with the
keyed version written by pylance at the table's own location.
"""

from __future__ import annotations

import io

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "lh538$t"


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({"lance.auto_cleanup.interval": "1"}, id="auto-cleanup"),
        pytest.param({"lance.compaction.max_source_fragments": "2"}, id="compaction"),
    ],
)
def test_a_restore_of_a_version_carrying_refused_config_is_refused_and_moves_nothing(real_ns_client: TestClient, config: dict[str, str | None]) -> None:
    assert real_ns_client.post("/v1/namespace/lh538/create", json={}).status_code == 200
    created = real_ns_client.post(f"/v1/table/{TABLE}/create", content=_ipc(pa.table({"id": pa.array([1, 2], pa.int64())})), headers=ARROW)
    assert created.status_code == 200, created.text
    location = str(created.json()["location"])
    lance.dataset(location).update_config(config)
    keyed = lance.dataset(location).version
    lance.dataset(location).delete_config_keys(list(config))
    latest = lance.dataset(location).version

    answer = real_ns_client.post(f"/v1/table/{TABLE}/restore", json={"version": keyed})

    assert answer.status_code == 400, answer.text
    assert answer.json()["code"] == 13, answer.json()
    after = lance.dataset(location)
    assert after.version == latest, f"a refused restore moved the table to v{after.version}"
    assert not [key for key in after.config() if key in config], after.config()
