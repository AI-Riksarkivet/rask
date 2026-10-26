"""Deletion protection guards dropping a table, not reclaiming its history (ADR 0150).

Lance keeps a version while a tag names it and a branch's files while the branch stands, so deleting the tag or the
branch is what makes them reclaimable (`lance_docs/lance/guide/tags_and_branches.md:61-67`, `:129-133`), and Lance's
answer to "keep this" is a tag (`lance_docs/lance/guide/data_evolution.md:170-179`). `version/delete`, tag delete and
branch delete are therefore history reclaim, like `maintenance/run`, and authorization alone governs them (their rung,
`can_drop`, is `fga_deps._OWNER_SUFFIX_RELATION`'s): none reads the protection record a drop consults, which is what
this test pins, with authorization off.

Driven through the real catalog app on a real `dir` namespace. The table is protected through the catalog's own door,
and its history is read back through the list doors, so each door is shown to remove its target and nothing else.
"""

from __future__ import annotations

import io
from typing import Any

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "lh300$t"


def _ipc(*ids: int) -> bytes:
    rows = pa.table({"id": pa.array(ids, pa.int64())})
    sink = io.BytesIO()
    with ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    return sink.getvalue()


@pytest.fixture
def protected(real_ns_client: TestClient) -> TestClient:
    """`lh300$t` at version 3, its version 1 tagged `release` and its head cut as the branch `staging`, then protected."""
    client = real_ns_client
    assert client.post("/v1/namespace/lh300/create", json={}).status_code == 200
    created = client.post(f"/v1/table/{TABLE}/create?mode=create", content=_ipc(1, 2), headers=ARROW)
    assert created.status_code == 200, created.text
    for ids in ((3,), (4,)):
        appended = client.post(f"/v1/table/{TABLE}/insert", content=_ipc(*ids), headers=ARROW)
        assert appended.status_code == 200, appended.text
    tagged = client.post(f"/v1/table/{TABLE}/tags/create", json={"tag": "release", "version": 1})
    assert tagged.status_code == 200, tagged.text
    branched = client.post(f"/v1/table/{TABLE}/branches/create", json={"name": "staging"})
    assert branched.status_code == 200, branched.text
    armed = client.post(f"/management/v1/table/{TABLE}/protection", json={"protected": True})
    assert (armed.status_code, armed.json().get("protected")) == (200, True), armed.text
    return client


def _history(client: TestClient) -> tuple[list[str], list[str], list[int]]:
    """The table's branches, its tags and main's versions, as the catalog's list doors answer them."""
    branches = client.post(f"/v1/table/{TABLE}/branches/list", json={})
    tags = client.post(f"/v1/table/{TABLE}/tags/list", json={})
    versions = client.post(f"/v1/table/{TABLE}/version/list", json={})
    for listed in (branches, tags, versions):
        assert listed.status_code == 200, listed.text
    return sorted(branches.json()["branches"]), sorted(tags.json()["tags"]), sorted(v["version"] for v in versions.json()["versions"])


@pytest.mark.parametrize(
    ("door", "body", "left"),
    [
        # `[2, 3)` is version 2 alone, since a `VersionRange` ends exclusive: version 1 is tagged and 3 is the head.
        pytest.param("version/delete", {"ranges": [{"start_version": 2, "end_version": 3}]}, (["staging"], ["release"], [1, 3]), id="version-delete"),
        pytest.param("tags/delete", {"tag": "release"}, (["staging"], [], [1, 2, 3]), id="tag-delete"),
        pytest.param("branches/delete", {"name": "staging"}, ([], ["release"], [1, 2, 3]), id="branch-delete"),
    ],
)
def test_a_protected_table_serves_the_door_that_reclaims_its_history(
    protected: TestClient, door: str, body: dict[str, Any], left: tuple[list[str], list[str], list[int]]
) -> None:
    answer = protected.post(f"/v1/table/{TABLE}/{door}", json=body)

    assert answer.status_code == 200, f"{door} refused a protected table: {answer.status_code} {answer.text}"
    assert _history(protected) == left
