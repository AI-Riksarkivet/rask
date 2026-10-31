"""[[LH-493]] An erasure is an obligation: the catalog records it, a bring-your-own job rewrites, the catalog verifies.

ADR 0163. The erasure door commits no data; the job deletes, compacts and rebuilds on its own compute; the
verification door judges what the job left on bytes and releases the obligation only when nothing it owes remains.
Driven through the erasure doors on a real pylance ``dir`` backend with the obligations on a local control root. The
job is the conftest's ``ErasureJob``, pylance straight on the table's files: the claims here are what the catalog
records and verifies. A stock client's commits through ``version/create`` are
``test_a_stock_client_commits_every_operation_through_version_create.py``'s claim.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import lance
import lance_namespace
import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import DescribeNamespaceRequest, LanceNamespace, NamespaceNotFoundError
from tables_by_name import open_by_name

from catalog.api.dependencies import get_clock, get_control_emitter, get_namespace, get_storage_options
from catalog.api.security import authenticate
from catalog.api.v1.endpoints import erasure as door
from catalog.core.config import Settings, get_settings
from catalog.core.identifiers import with_minted_uuid
from catalog.services.dataplane import create_table, read_arrow_body
from service_kit.control_events import CatalogControlEvent
from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal
from service_kit.lakehouse.base_registry import BaseRegistry
from service_kit.lakehouse.derived_tables import EDGES
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_SUBJECT = "alice"
_PREDICATE = f"pii = '{_SUBJECT}'"
_TABLE_ID = ["db", "subjects"]


class _Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _control(root: str | Path) -> Path:
    """The control root beside the data root ``root``, which the catalog's settings name in ``_catalog``."""
    return Path(root).parent / "control"


def _create(ns: LanceNamespace, root: str, table_id: list[str]) -> None:
    """``table_id`` through the catalog's create path at ``root``: the subject beside bob in one fragment, ``pii`` BTREE-indexed.

    The create takes the table's claim on the control root, which mints the uuid every record kept for it is keyed by.
    """
    rows = pa.table({"pii": pa.array(["bob", _SUBJECT])})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    body = read_arrow_body(sink.getvalue().to_pybytes(), max_bytes=1 << 20)
    registry = BaseRegistry(control_root=str(_control(root)), storage_options={})
    create_table(ns, StorageOptions(), table_id, body, root=root, mode="create", registry=registry, holder="$".join(table_id))
    open_by_name(ns, StorageOptions(), table_id).create_scalar_index("pii", "BTREE")


def _subjects(root: Path) -> LanceNamespace:
    ns = lance_namespace.connect("dir", {"root": str(root)})
    ns.create_namespace(lance_namespace.CreateNamespaceRequest(id=_TABLE_ID[:-1], properties=with_minted_uuid(None)))
    _create(ns, str(root), _TABLE_ID)
    return ns


class _ControlLane:
    """The control lane as the events emitted on it."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)


@contextmanager
def _catalog(ns: LanceNamespace, control: Path, clock: _Clock, *, lane: _ControlLane | None = None) -> Generator[TestClient]:
    """The erasure doors, the owner ``dex~olivia`` signed in, authorization off; ``lane`` takes the control events."""
    settings = Settings(LANCE_S3_ACCESS_KEY_ID="k", LANCE_S3_SECRET_ACCESS_KEY="s", LANCE_CONTROL_ROOT=str(control))
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.router)
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: ns
    application.dependency_overrides[get_storage_options] = lambda: StorageOptions()
    application.dependency_overrides[authenticate] = lambda: PersonPrincipal(
        principal=Principal(idp_id="dex", subject="olivia"), claims=IDToken(iss="i", sub="olivia", aud="lance", exp=1, iat=1)
    )
    application.dependency_overrides[get_clock] = lambda: clock
    if lane is not None:
        application.dependency_overrides[get_control_emitter] = lambda: lane
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


def _record(client: TestClient, table: str = "db$subjects") -> dict[str, Any]:
    response = client.post(f"/management/v1/table/{table}/erasure", json={"predicate": _PREDICATE})
    assert response.status_code == 202, response.text
    return response.json()


def _verify(client: TestClient, obligation_id: str, predicate: str = _PREDICATE) -> Any:  # noqa: ANN401 — an httpx.Response
    return client.post(f"/management/v1/table/db$subjects/erasure/{obligation_id}/verify", json={"predicate": predicate})


def _view(client: TestClient, obligation_id: str) -> dict[str, Any]:
    """The obligation as the list door shows it."""
    (view,) = [entry for entry in client.get("/management/v1/erasures").json()["obligations"] if entry["obligation_id"] == obligation_id]
    return view


def test_the_door_records_an_obligation_and_commits_nothing(tmp_path: Path) -> None:
    """The door is the requester's act; every rewrite is the job's. Its record keeps no trace of the subject."""
    ns = _subjects(tmp_path / "data")
    location = open_by_name(ns, StorageOptions(), _TABLE_ID).uri
    history = [entry["version"] for entry in lance.dataset(location).versions()]

    with _catalog(ns, tmp_path / "control", _Clock()) as client:
        obligation = _record(client)

    expected = ("open", {"main": history[-1]}, "user:dex~olivia", [])
    assert (obligation["state"], obligation["refs"], obligation["requested_by"], obligation["open_surfaces"]) == expected
    after = lance.dataset(location)
    assert ([entry["version"] for entry in after.versions()], after.count_rows(filter=_PREDICATE)) == (history, 1)
    recorded = (tmp_path / "control" / "_erasure_obligations" / f"{obligation['obligation_id']}.json").read_bytes()
    assert _SUBJECT.encode() not in recorded


@pytest.mark.parametrize(
    ("steps", "named"),
    [
        pytest.param(("compact", "drop_indices"), "main", id="the-delete"),
        pytest.param(("delete", "drop_indices"), "compact:main", id="the-compaction"),
        pytest.param(("delete", "compact"), "index:main", id="the-index-drop"),
        pytest.param(("delete", "compact", "rebuild_indices"), "index:main", id="an-index-rebuilt-rather-than-dropped"),
    ],
)
def test_a_job_that_skips_a_surface_leaves_the_obligation_open_naming_it(tmp_path: Path, erasure_job: Any, steps: tuple[str, ...], named: str) -> None:  # noqa: ANN401 — conftest.ErasureJob
    """Each step the job owes is judged on the head's bytes, never taken on the job's word: an index it rebuilt is a
    segment committed since the obligation was recorded, whose provenance is the job's own claim, so it proves nothing."""
    ns = _subjects(tmp_path / "data")
    location = open_by_name(ns, StorageOptions(), _TABLE_ID).uri
    with _catalog(ns, tmp_path / "control", _Clock()) as client:
        obligation = _record(client)
        job = erasure_job(location, _PREDICATE)
        for step in steps:
            getattr(job, step)()
        verified = _verify(client, obligation["obligation_id"])

    assert verified.status_code == 200, verified.text
    standing = verified.json()["obligation"]
    owed = [surface["surface"] for surface in standing["open_surfaces"] if surface["outcome"] == "failed" and surface["surface"] != "verify"]
    assert (standing["state"], owed) == ("open", [named]), standing["open_surfaces"]


type _Refused = Callable[[], Any]


def _another_predicate(client: TestClient, ns: LanceNamespace, erasure_job: Any) -> tuple[str, _Refused]:  # noqa: ANN401 — conftest.ErasureJob
    """A predicate nothing matches would verify clean and release the obligation while the subject stays."""
    obligation = _record(client)["obligation_id"]
    return obligation, lambda: _verify(client, obligation, predicate="pii = 'nobody'")


def _another_tables(client: TestClient, ns: LanceNamespace, erasure_job: Any) -> tuple[str, _Refused]:  # noqa: ANN401 — conftest.ErasureJob
    """An obligation is verified on the table it was recorded on, whatever table the caller may maintain."""
    root = open_by_name(ns, StorageOptions(), _TABLE_ID).uri.rsplit("/", 1)[0]  # the root `subjects` was declared under
    _create(ns, root, ["db", "others"])
    obligation = _record(client, "db$others")["obligation_id"]
    return obligation, lambda: _verify(client, obligation)


def _a_released_one(client: TestClient, ns: LanceNamespace, erasure_job: Any) -> tuple[str, _Refused]:  # noqa: ANN401 — conftest.ErasureJob
    """A released obligation's digest is gone, and nothing reopens it."""
    obligation = _record(client)["obligation_id"]
    erasure_job(open_by_name(ns, StorageOptions(), _TABLE_ID).uri, _PREDICATE).run()
    assert _verify(client, obligation).json()["obligation"]["state"] == "released"
    return obligation, lambda: _verify(client, obligation)


@pytest.mark.parametrize(
    ("arrange", "answer"),
    [
        pytest.param(_another_predicate, (400, 13), id="a-predicate-other-than-the-obligations"),
        pytest.param(_another_tables, (409, 19), id="another-tables-obligation"),
        pytest.param(_a_released_one, (409, 19), id="a-released-obligation"),
    ],
)
def test_a_verification_the_obligation_does_not_admit_is_refused_and_changes_nothing(
    tmp_path: Path,
    erasure_job: Any,  # noqa: ANN401 — conftest.ErasureJob
    arrange: Callable[[TestClient, LanceNamespace, Any], tuple[str, _Refused]],
    answer: tuple[int, int],
) -> None:
    ns = _subjects(tmp_path / "data")
    with _catalog(ns, tmp_path / "control", _Clock()) as client:
        obligation, attempt = arrange(client, ns, erasure_job)
        before = _view(client, obligation)
        response = attempt()
        after = _view(client, obligation)

    assert (response.status_code, response.headers["content-type"]) == (answer[0], "application/problem+json"), response.text
    assert response.json()["code"] == answer[1]
    assert after == before


def test_each_tag_the_verification_drops_is_announced_on_the_control_lane(tmp_path: Path, erasure_job: Any) -> None:  # noqa: ANN401 — conftest.ErasureJob
    """[[LH-332]] Once every head is clean the verification drops each tag pinning the subject, and announces each one as
    the tag door announces a deletion: untargeted, naming the tag and the ref it pinned, beside the obligation."""
    ns = _subjects(tmp_path / "data")
    location = open_by_name(ns, StorageOptions(), _TABLE_ID).uri
    lance.dataset(location).tags.create("trained", 2)
    lance.dataset(location).create_branch("work", 2)
    lance.dataset(location).tags.create("trained-on-work", ("work", 2))
    lane = _ControlLane()
    with _catalog(ns, tmp_path / "control", _Clock(), lane=lane) as client:
        obligation = _record(client)["obligation_id"]
        erasure_job(location, _PREDICATE).run()
        verified = _verify(client, obligation)

    assert verified.status_code == 200, verified.text
    table = f"table:{verified.json()['obligation']['table_uuid']}"
    announced = sorted(
        (event.object_id, event.actor, event.extra.get("tag"), event.extra.get("branch"), event.extra.get("obligation_id"))
        for event in lane.events
        if event.action == "table_tag_deleted"
    )
    assert announced == [
        (table, "user:dex~olivia", "trained", None, obligation),
        (table, "user:dex~olivia", "trained-on-work", "work", obligation),
    ]


def test_an_obligation_following_one_the_catalog_never_recorded_is_refused_and_nothing_is_recorded(tmp_path: Path) -> None:
    """[[LH-332]] An obligation following no recorded one would leave the source's obligation open for ever while its
    requester reads this table as followed, so the door refuses it before it disarms or records anything."""
    ns = _subjects(tmp_path / "data")
    with _catalog(ns, tmp_path / "control", _Clock()) as client:
        response = client.post("/management/v1/table/db$subjects/erasure", json={"predicate": _PREDICATE, "follows": "0" * 32})
        listed = client.get("/management/v1/erasures").json()["obligations"]

    assert (response.status_code, response.json()["code"], listed) == (400, 13, []), response.text


_PROVENANCE = "provenance"


@pytest.fixture
def keeps_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog keeps a provenance namespace, as the chart has it do whenever it deploys lineage; requested before
    ``governed_catalog`` so the app boots with it."""
    monkeypatch.setenv("LANCE_PROVENANCE_NAMESPACE", _PROVENANCE)


def _record_edge(root: Path, source: str, derived: str) -> None:
    """Record on the edge list a run writing ``derived`` from ``source``, as lineage records one, once the catalog's boot has
    made the provenance namespace: lineage writes the edge list, and no door of the catalog's does."""
    ns = lance_namespace.connect("dir", {"root": str(root)})
    deadline = time.monotonic() + 30
    while True:
        try:
            ns.describe_namespace(DescribeNamespaceRequest(id=[_PROVENANCE]))
            break
        except NamespaceNotFoundError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    edge = pa.table(
        {
            "in_table": [source],
            "out_table": [derived],
            "out_name": ["db$derived"],
            "received_at": pa.array([datetime.now(UTC)], pa.timestamp("us", tz="UTC")),
        }
    )
    registry = BaseRegistry(control_root=str(root), storage_options={})
    create_table(ns, StorageOptions(), [_PROVENANCE, EDGES], edge, root=str(root), mode="create", registry=registry, holder=f"{_PROVENANCE}${EDGES}")


@pytest.fixture
def derived_estate(keeps_provenance: None, governed_catalog: Any, tmp_path: Path) -> tuple[Any, str, str]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's ``db$source`` and ``db$derived``, the second recorded on the edge list as written from the first, and her
    obligation on the first; bob maintains ``db$source`` and holds nothing on ``db$derived``. Answers the catalog, the
    obligation's id and ``db$derived``'s uuid."""
    alice, client = governed_catalog.bearer("alice"), governed_catalog.client
    assert client.post("/v1/namespace/db/create", json={}, headers=alice).status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))
    for table in ("source", "derived"):
        created = client.post(f"/v1/table/db${table}/create", content=rows, headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert created.status_code == 200, created.text
    granted = client.post("/management/v1/table/db$source/access/grant", json={"user": "dex~bob", "relation": "maintainer"}, headers=alice)
    assert granted.status_code == 200, granted.text
    uuid_of = {
        table: client.post(f"/v1/table/db${table}/describe", json={}, headers=alice).json()["properties"]["rask.uuid"] for table in ("source", "derived")
    }
    _record_edge(tmp_path, uuid_of["source"], uuid_of["derived"])
    recorded = client.post("/management/v1/table/db$source/erasure", json={"predicate": "id = 99"}, headers=alice)
    assert recorded.status_code == 202, recorded.text
    return governed_catalog, recorded.json()["obligation_id"], uuid_of["derived"]


def _derived(surfaces: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(surface["surface"], surface["outcome"]) for surface in surfaces if surface["surface"].startswith("derived")}


def _verified(catalog: Any, obligation: str, reader: str) -> set[tuple[str, str]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """The derived surfaces the verification shows ``reader``, in its report and in the obligation it answers."""
    answer = catalog.client.post(f"/management/v1/table/db$source/erasure/{obligation}/verify", json={"predicate": "id = 99"}, headers=catalog.bearer(reader))
    assert answer.status_code == 200, answer.text
    return _derived([*answer.json()["report"]["surfaces"], *answer.json()["obligation"]["open_surfaces"]])


def _listed(catalog: Any, obligation: str, reader: str) -> set[tuple[str, str]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """The derived surfaces the listing shows ``reader`` of the obligation."""
    listed = catalog.client.get("/management/v1/erasures", headers=catalog.bearer(reader)).json()["obligations"]
    (view,) = [entry for entry in listed if entry["obligation_id"] == obligation]
    return _derived(view["open_surfaces"])


@pytest.mark.parametrize(
    ("verified_by_alice", "door_shows", "reader", "named"),
    [
        pytest.param(False, _verified, "alice", True, id="the-verification-names-it-to-a-reader-who-may-see-it"),
        pytest.param(False, _verified, "bob", False, id="the-verification-counts-it-for-a-reader-who-may-not"),
        pytest.param(True, _listed, "bob", False, id="the-listing-counts-it-for-a-reader-who-may-not"),
    ],
)
def test_a_derived_table_is_named_only_to_a_reader_who_may_see_it(
    derived_estate: tuple[Any, str, str],
    verified_by_alice: bool,
    door_shows: Callable[[Any, str, str], set[tuple[str, str]]],
    reader: str,
    named: bool,
) -> None:
    """[[LH-332]] A derived table holds the obligation open whoever reads it, and its uuid reaches only a reader holding
    ``can_get_metadata`` on it, as lineage's derived door shows a table: a maintainer of the source who may not see it
    learns that a table they may not see keeps the obligation open, and nothing about which. The listing shows what the
    last verification left open, so its case has alice verify first."""
    catalog, obligation, derived = derived_estate
    if verified_by_alice:
        _verified(catalog, obligation, "alice")

    shown = door_shows(catalog, obligation, reader)

    assert shown == {(f"derived:{derived}" if named else "derived", "failed")}


def test_an_obligation_open_past_its_deadline_is_listed_overdue_and_nothing_acts_on_it(tmp_path: Path) -> None:
    """A missed deadline is visible on the obligation; the catalog never does the job's work itself."""
    ns = _subjects(tmp_path / "data")
    location = open_by_name(ns, StorageOptions(), _TABLE_ID).uri
    clock = _Clock()
    with _catalog(ns, tmp_path / "control", clock) as client:
        _record(client)
        clock.now += timedelta(days=31)
        listed = client.get("/management/v1/erasures").json()["obligations"]

    assert [(entry["state"], entry["overdue"]) for entry in listed] == [("open", True)]
    assert lance.dataset(location).count_rows(filter=_PREDICATE) == 1
