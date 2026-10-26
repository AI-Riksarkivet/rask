"""A malformed branch name is the caller's mistake, refused before anything is written ([[LH-283]]).

`lance_docs/lance/format/table/branch_tag.md` § "Branch Name" is the grammar, and pylance validates a name against it
before it writes: measured on 13.0.0 over a `dir` table and a moto S3 one, each name below that breaks one of its rules
answers ``Ref is invalid: …``, which the door maps to InvalidInput, and writes no object. The grammar admits a ``.``
segment (rule 5 allows ``.`` in a segment, rule 4 forbids only ``..``), and the object store's path parser refuses one
(``LanceError(IO): Error parsing Path … illegal character sequence "."``, which carries no code), so the door refuses a
``.`` segment itself. A name can also break no rule and still be one the store cannot hold: its ref file is
``_refs/branches/<name>.json`` with the name URL-encoded (§ "Branch Metadata Path"; ``/`` and every non-ASCII byte become
``%XX``, measured on 13.0.0), and pylance writes ``tree/<name>/`` before it. Measured on RustFS 1.0.3, a ref file longer
than one 255-byte key segment is refused after the branch's directory is written, so the door refuses such a name too.

A refusal that came after a write would leave ``tree/<name>/`` behind, and that residue answers a retry, or a create of
the valid name the bad one collapses to, with pylance's bug-report text (measured on pylance 12.0.0, which wrote before
it validated). So each name is sent twice and then the name it collapses to, through the real door on a real `dir`
table, and nothing may be left under the table's branch directories in between.

The cases are one name per rule of the grammar (rule 7, ``main``, is the door's own 23, pinned by
`tests/unit/test_a_branch_name_the_backend_cannot_use_is_a_caller_error.py`), plus the path steps. The grammar's names
walk one path through the catalog and a different rule each through pylance's validator, so a pylance that writes before
it validates on any one rule fails here.
"""

from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import urlsplit

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "b1$t"


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _create(client: TestClient, name: str) -> tuple[int, int | None]:
    response = client.post(f"/v1/table/{TABLE}/branches/create", json={"name": name})
    return response.status_code, response.json().get("code")


def _branch_objects(table_dir: Path) -> list[str]:
    """Everything under the table's branch directories (``tree/``) and its refs (``_refs/``)."""
    return sorted(str(path.relative_to(table_dir)) for directory in ("tree", "_refs") for path in (table_dir / directory).rglob("*"))


@pytest.mark.parametrize(
    ("name", "collapses_to"),
    [
        pytest.param("", "a", id="empty"),
        pytest.param("/a", "a", id="leading-slash"),
        # Only a '/', which names `tree/` itself: pylance 12.0.0 left `tree/_versions/` for it.
        pytest.param("/", "a", id="only-a-slash"),
        pytest.param("a//b", "a/b", id="consecutive-slashes"),
        # Rule 4 forbids two things, a '..' and a '\'.
        pytest.param("a..b", "a", id="double-dot"),
        pytest.param("a\\b", "a/b", id="backslash"),
        pytest.param("a b", "a", id="invalid-character"),
        pytest.param("a.lock", "a", id="lock-suffix"),
        # A '.' that is the whole name, and one that is a segment inside it.
        pytest.param(".", "a", id="dot"),
        pytest.param("a/./b", "a/b", id="dot-segment"),
        pytest.param("a" * 251, "a", id="ref-file-of-256-bytes"),
        pytest.param("ä" * 85, "a", id="non-ascii-ref-file-of-515-bytes"),
        pytest.param("a/" * 84 + "a", "a", id="nested-ref-file-of-342-bytes"),
    ],
)
def test_a_malformed_branch_name_is_refused_on_every_try_and_leaves_nothing_behind(real_ns_client: TestClient, name: str, collapses_to: str) -> None:
    rows = pa.table({"id": pa.array(range(3), pa.int64())})
    assert real_ns_client.post("/v1/namespace/b1/create", json={}).status_code == 200
    assert real_ns_client.post(f"/v1/table/{TABLE}/create", content=_ipc(rows), headers=ARROW).status_code == 200
    # The described location is a `file://` URI; the table's directory is its path.
    table_dir = Path(urlsplit(real_ns_client.post(f"/v1/table/{TABLE}/describe", json={}).json()["table_uri"]).path)
    client = TestClient(real_ns_client.app, raise_server_exceptions=False)

    answers = [_create(client, name), _create(client, name)]
    left = _branch_objects(table_dir)
    answers.append(_create(client, collapses_to))

    assert (answers, left) == ([(400, 13), (400, 13), (200, None)], [])
