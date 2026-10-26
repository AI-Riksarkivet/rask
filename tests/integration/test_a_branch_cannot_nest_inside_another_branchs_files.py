"""A branch's directory holds only that branch's files ([[LH-203]]).

`lance_docs/lance/format/table/branch_tag.md` § "Branch Dataset Layout" joins a branch name verbatim onto ``tree/``, and
§ "Branch Name" allows ``/`` and ``_``. So ``a/b`` lives inside ``tree/a/``, and ``a/_versions`` lives
inside ``tree/a/_versions/``, which is ``a``'s own version directory. Lance reads whatever lies under a
branch's directory as that branch's, which the catalog's create, reclaim and delete doors and the sweep
all have to answer for. Measured on pylance 12.0.0 before the fix: a reclaim of ``a`` deleted
``a/_versions``'s manifests (a cold read failed ``Not found``), and deleting ``a`` removed only its ref.

The legacy pair is planted through pylance, because the create door now refuses it; a table made before
that refusal is the only place such a pair exists. Driven through the real catalog app on the real ``dir``
backend, and survival is read from a cold interpreter: a reclaim's damage shows on the next open, not on
the handle that did it.
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient

from maintenance.services.expiry import ExpiryPolicy, ExpiryTarget, expire_versions


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "b1$t"


def _rows(n: int) -> pa.Table:
    return pa.table({"id": pa.array(range(n), pa.int64())})


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.fixture
def legacy(real_ns_client: TestClient) -> tuple[TestClient, str]:
    """``a`` cut from main and moved on, and ``a/_versions`` cut from main v1 so its numbers sit below a's."""
    assert real_ns_client.post("/v1/namespace/b1/create", json={}).status_code == 200
    assert real_ns_client.post(f"/v1/table/{TABLE}/create?mode=overwrite", content=_ipc(_rows(3)), headers=ARROW).status_code == 200
    location = real_ns_client.post(f"/v1/table/{TABLE}/describe", json={}).json()["table_uri"]
    lance.write_dataset(_rows(3), location, mode="append")
    lance.dataset(location).create_branch("a").insert(_rows(2))
    lance.dataset(location).checkout_version(("a", None)).insert(_rows(2))
    lance.dataset(location).create_branch("a/_versions", 1)
    lance.dataset(location).create_branch("p/q")
    return real_ns_client, location


def _cold_rows(location: str, branch: str) -> subprocess.CompletedProcess[str]:
    code = f"import lance; print(lance.dataset({location!r}).checkout_version(({branch!r}, None)).count_rows())"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    ("name", "status", "code"),
    [
        pytest.param("b/_indices", 400, 13, id="reserved-segment"),
        pytest.param("a/b", 409, 19, id="inside-an-existing-branch"),
        pytest.param("p", 409, 19, id="around-an-existing-branch"),
    ],
)
def test_a_name_whose_directory_would_share_files_is_refused_at_create(legacy: tuple[TestClient, str], name: str, status: int, code: int) -> None:
    client, location = legacy

    response = client.post(f"/v1/table/{TABLE}/branches/create", json={"name": name})

    assert (response.status_code, response.json().get("code")) == (status, code), response.text
    assert name not in lance.dataset(location).branches.list()


def _sweep(client: TestClient, location: str) -> None:
    del client
    # A positive threshold, as the sweep runs it (MAINTENANCE_OLDER_THAN_DAYS forbids 0), passed in a few
    # milliseconds so every planted version is past it.
    time.sleep(0.05)
    expire_versions(ExpiryTarget(uri=f"{location}/tree/a", storage_options={}), ExpiryPolicy(older_than=timedelta(milliseconds=1)))


def _gc_door(client: TestClient, location: str) -> None:
    del location
    response = client.post(f"/management/v1/table/{TABLE}/maintenance/run?branch=a", json={"retain_versions": 1})
    # Either answer leaves the claim to the cold read; anything else means the door was never reached.
    assert response.status_code in {200, 409}, response.text


def _version_delete(client: TestClient, location: str) -> None:
    del location
    response = client.post(f"/v1/table/{TABLE}/version/delete", json={"branch": "a", "ranges": [{"start_version": 1, "end_version": 2}]})
    # Refused before anything is deleted: a reclaim of `a` would take `a/_versions`' manifests as its own history.
    assert (response.status_code, response.json().get("code")) == (409, 19), response.text


@pytest.mark.parametrize(
    "reclaim", [pytest.param(_sweep, id="sweep"), pytest.param(_gc_door, id="maintenance-run"), pytest.param(_version_delete, id="version-delete")]
)
def test_a_reclaim_of_a_branch_leaves_the_branch_inside_it_readable(legacy: tuple[TestClient, str], reclaim) -> None:  # noqa: ANN001
    client, location = legacy

    reclaim(client, location)

    cold = _cold_rows(location, "a/_versions")
    assert (cold.returncode, cold.stdout.strip()) == (0, "3"), cold.stderr[-300:]


@pytest.mark.parametrize(
    ("fork", "status", "left"),
    [
        pytest.param(False, (200, None), ["p/q"], id="nested-branches-go-with-it"),
        # A branch cut from `a` pins it, and pylance refuses `a` only after `a/_versions` is gone, so the
        # refusal has to come before the first delete.
        pytest.param(True, (409, 19), ["a", "a/_versions", "c", "p/q"], id="a-fork-refuses-the-whole-delete"),
    ],
)
def test_deleting_a_branch_leaves_nothing_under_its_directory(
    legacy: tuple[TestClient, str], fork: bool, status: tuple[int, int | None], left: list[str]
) -> None:
    client, location = legacy
    if fork:
        lance.dataset(location).create_branch("c", ("a", None))

    response = client.post(f"/v1/table/{TABLE}/branches/delete", json={"name": "a"})

    answer = (response.status_code, response.json().get("code"))
    assert (answer, sorted(lance.dataset(location).branches.list())) == (status, left), response.text
    # The described location is a `file://` URI; the directory is its path.
    tree_a = Path(urlsplit(location).path) / "tree" / "a"
    assert tree_a.exists() is fork, sorted(str(p) for p in tree_a.rglob("*"))


def test_two_creates_of_nesting_names_on_one_table_never_both_land(legacy: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """[[LH-413]] ``n`` and ``n/m`` are two ref files no conditional write on a ref makes collide
    (`lance_docs/lance/format/table/branch_tag.md` § Branch Metadata Path), so each create judging a listing the other has
    not changed yet would land both. A rival create of ``n/m`` is sent while ``n``'s create is inside pylance."""
    client, location = legacy
    create_branch = lance.LanceDataset.create_branch
    rival: list[tuple[int, int | None]] = []
    # Set before the rival is sent, so a rival that reaches pylance itself sends no rival of its own.
    raced: list[bool] = []

    def _create_then_race(
        self: lance.LanceDataset,
        branch: str,
        reference: int | str | tuple[str | None, int | None] | None = None,
        storage_options: dict[str, str] | None = None,
    ) -> lance.LanceDataset:
        if not raced:
            raced.append(True)
            answered = client.post(f"/v1/table/{TABLE}/branches/create", json={"name": "n/m"})
            rival.append((answered.status_code, answered.json().get("code")))
        return create_branch(self, branch, reference, storage_options)

    monkeypatch.setattr(lance.LanceDataset, "create_branch", _create_then_race)

    answer = client.post(f"/v1/table/{TABLE}/branches/create", json={"name": "n"})

    assert answer.status_code == 200, answer.text
    assert rival == [(409, 14)], f"a create of a name nesting with one being created was not refused: {rival}"
    assert sorted(name for name in lance.dataset(location).branches.list() if name.startswith("n")) == ["n"]
