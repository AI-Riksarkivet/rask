"""End-to-end catalog round-trip against a moto-mocked S3 (no MinIO needed).

Runs the real app + native backend + pylance data plane against an in-process
moto S3 server, exercising the full create → insert → count → query path on fake
Lance data. This is the deterministic, infra-free counterpart to the Docker e2e.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from moto.server import ThreadedMotoServer


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
BUCKET = "lance-moto"


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


@pytest.fixture(scope="module")
def moto_endpoint() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    url = f"http://{host}:{port}"
    s3 = boto3.client(
        "s3",
        endpoint_url=url,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )
    s3.create_bucket(Bucket=BUCKET)
    yield url
    server.stop()


@pytest.fixture
def moto_client(moto_endpoint: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{BUCKET}",
        "LANCE_S3_ENDPOINT": moto_endpoint,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_S3_ALLOW_HTTP": "true",
        "LANCE_OIDC_ENABLED": "false",
        "LANCE_FGA_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)  # auto-restored on teardown -> order-independent

    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_catalog_roundtrip_on_moto_s3(moto_client: TestClient) -> None:
    assert moto_client.post("/v1/namespace/m1/create", json={}).status_code == 200

    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "name": ["a", "b", "c"]})
    created = moto_client.post("/v1/table/m1$t/create?mode=overwrite", content=_ipc(rows), headers=ARROW)
    assert created.status_code == 200, created.text
    assert created.json()["location"].startswith(f"s3://{BUCKET}/")

    assert (
        moto_client.post(
            "/v1/table/m1$t/insert?mode=append",
            content=_ipc(pa.table({"id": pa.array([4], pa.int64()), "name": ["d"]})),
            headers=ARROW,
        ).status_code
        == 200
    )
    assert int(moto_client.post("/v1/table/m1$t/count_rows", json={}).text) == 4

    query = moto_client.post("/v1/table/m1$t/query", json={"k": 10, "filter": "id >= 2", "vector": {}})
    assert query.headers["content-type"].startswith("application/vnd.apache.arrow.file")
    assert ipc.open_file(pa.BufferReader(query.content)).read_all().num_rows == 3


# --------------------------------------------------------------------------- #
# §4: merge_insert ensures a BTREE on its merge key (implicit DDL, idempotent, best-effort)
# --------------------------------------------------------------------------- #


def _create(client: TestClient, ident: str, rows: pa.Table, query: str = "") -> Any:
    """Create a table THROUGH its parent — a table whose namespace does not exist is refused (#118).

    Every test below used to post straight at the Arrow create door and get a 200, because that door
    was the one create path with no parent guard on it. Creating the namespace first is what a real
    caller has to do, so the tests do it too.
    """
    namespace = ident.rsplit("$", 1)[0]
    made = client.post(f"/v1/namespace/{namespace}/create", json={})
    assert made.status_code in {200, 409}, made.text
    return client.post(f"/v1/table/{ident}/create{query}", content=_ipc(rows), headers=ARROW)


def _merge(client: TestClient, table: str, rows: pa.Table) -> Any:
    return client.post(
        f"/v1/table/{table}/merge_insert?on=id&when_matched_update_all=true&when_not_matched_insert_all=true",
        content=_ipc(rows),
        headers=ARROW,
    )


def _index_columns(client: TestClient, table: str) -> list[tuple[str, list[str]]]:
    listing = client.post(f"/v1/table/{table}/index/list", json={})
    assert listing.status_code == 200, listing.text
    # index_type casing is backend-flavored ("BTree" from the dir backend) — normalize for asserting.
    return [(ix["index_type"].upper(), ix["columns"]) for ix in listing.json().get("indexes", [])]


def test_merge_insert_builds_btree_on_the_merge_key_exactly_once(moto_client: TestClient) -> None:
    """Two consecutive merges on the same (table, on) trigger EXACTLY ONE index build — the list-first
    guard is load-bearing (create_scalar_index defaults replace=True: an unconditional build would
    full-rebuild the column on every upsert, turning the accelerator into a regression).

    A DEDICATED MonkeyPatch context scopes the spy: the function-scoped `monkeypatch` fixture is the
    SAME instance the moto_client fixture used for its env vars, so undo()/teardown ordering with it
    is a foot-gun (review 2026-07-10)."""
    import catalog.services.dataplane as dp

    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": ["a", "b", "c"]})
    assert _create(moto_client, "mk$t", rows).status_code == 200

    builds: list[str] = []
    real_call = dp.native.call

    def counting_call(ns: Any, method: str, *args: Any) -> Any:
        if method == "create_table_scalar_index":
            builds.append(method)
        return real_call(ns, method, *args)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dp.native, "call", counting_call)
        first = _merge(moto_client, "mk$t", pa.table({"id": pa.array([2, 4], pa.int64()), "v": ["B", "d"]}))
        assert first.status_code == 200, first.text
        assert ("BTREE", ["id"]) in _index_columns(moto_client, "mk$t")  # visible via the list endpoint
        second = _merge(moto_client, "mk$t", pa.table({"id": pa.array([5], pa.int64()), "v": ["e"]}))
        assert second.status_code == 200, second.text
    assert builds == ["create_table_scalar_index"]  # ONE build across two merges (idempotence)
    assert int(moto_client.post("/v1/table/mk$t/count_rows", json={}).text) == 5  # upsert applied


@pytest.mark.parametrize("failing_method", ["list_table_indices", "create_table_scalar_index"])
def test_merge_insert_survives_index_ensure_failure(moto_client: TestClient, failing_method: str) -> None:
    """The index is an accelerator: a failure in EITHER half of the ensure path — the list-first
    check or the build itself (the spec's named CreateIndex-commit-conflict case) — must never fail
    the merge that already committed. Parametrized so a refactor splitting the two out of one
    try-block can't silently lose the build-failure coverage (review 2026-07-10)."""
    import catalog.services.dataplane as dp

    table = f"mf{failing_method[:4]}$t"
    rows = pa.table({"id": pa.array([1], pa.int64()), "v": ["a"]})
    create = _create(moto_client, table, rows)
    assert create.status_code == 200, create.text

    real_call = dp.native.call

    def failing_call(ns: Any, method: str, *args: Any) -> Any:
        if method == failing_method:
            raise RuntimeError("index backend down")
        return real_call(ns, method, *args)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(dp.native, "call", failing_call)
        merged = _merge(moto_client, table, pa.table({"id": pa.array([2], pa.int64()), "v": ["b"]}))
        assert merged.status_code == 200, merged.text  # the write result, not the accelerator's
    assert int(moto_client.post(f"/v1/table/{table}/count_rows", json={}).text) == 2


# --------------------------------------------------------------------------- #
# §4: create_table dual-write compensation — a failed owner grant must not strand the table
# --------------------------------------------------------------------------- #


def test_create_compensates_when_the_owner_grant_fails(moto_client: TestClient) -> None:
    """Grant fails after the Lance write → the create 5xxs AND the table is deleted, so the client's
    retry starts clean instead of hitting 'already exists' on a forever-ownerless table."""
    import catalog.api.v1.endpoints.data as data_ep
    from lance_namespace import ServiceUnavailableError

    async def failing_seed(*_a: object, **_kw: object) -> None:
        raise ServiceUnavailableError("fga down mid-create")  # what a real FGA outage surfaces as

    rows = pa.table({"id": pa.array([1], pa.int64())})
    # The parent lands BEFORE the outage: namespace create seeds ownership too, so creating it under
    # the patch would fail the setup rather than the behaviour under test.
    assert moto_client.post("/v1/namespace/comp/create", json={}).status_code == 200
    with pytest.MonkeyPatch.context() as mp:  # dedicated context — never the fixture's instance
        mp.setattr(data_ep.fga_deps, "seed_ownership", failing_seed)
        failed = moto_client.post("/v1/table/comp$t/create", content=_ipc(rows), headers=ARROW)
    assert failed.status_code == 503, failed.text  # the grant failure surfaces (compensation ran)
    # the compensation deleted the half-created table…
    assert moto_client.post("/v1/table/comp$t/describe", json={}).status_code == 404
    # …so, with FGA "recovered", the plain retry succeeds
    retried = _create(moto_client, "comp$t", rows)
    assert retried.status_code == 200, retried.text


def test_create_existok_never_compensates_away_a_kept_table(moto_client: TestClient) -> None:
    """ExistOk may have KEPT a pre-existing table this request never wrote — compensation deleting it
    would destroy someone else's data. ExistOk is retry-safe end-to-end anyway (the retry re-runs the
    grant), so it must never trigger the delete."""
    import catalog.api.v1.endpoints.data as data_ep
    from lance_namespace import ServiceUnavailableError

    rows = pa.table({"id": pa.array([1, 2], pa.int64())})
    assert _create(moto_client, "keep$t", rows).status_code == 200

    async def failing_seed(*_a: object, **_kw: object) -> None:
        raise ServiceUnavailableError("fga down mid-create")

    with pytest.MonkeyPatch.context() as mp:  # dedicated context — never the fixture's instance
        mp.setattr(data_ep.fga_deps, "seed_ownership", failing_seed)
        failed = _create(moto_client, "keep$t", rows, "?mode=exist_ok")
    assert failed.status_code == 503, failed.text
    # the pre-existing table SURVIVED the failed ExistOk…
    assert int(moto_client.post("/v1/table/keep$t/count_rows", json={}).text) == 2
    # …and the ExistOk retry heals (grant re-runs against the kept table).
    healed = _create(moto_client, "keep$t", rows, "?mode=exist_ok")
    assert healed.status_code == 200, healed.text


def test_compensation_matrix_never_drops_a_replaced_or_kept_table() -> None:
    """The Overwrite-of-existing arm can't be driven in this harness (needs FGA on), so the decision
    is a pure function and pinned here: compensation may drop ONLY a fresh-id create — never an
    ExistOk (may have kept a table) and never an Overwrite that replaced one (its time-travel
    history would be destroyed by a transient FGA blip — review 2026-07-10)."""
    from catalog.api.v1.endpoints.data import _compensation_allowed

    assert _compensation_allowed(None, overwrote_existing=False) is True  # plain create, fresh id
    assert _compensation_allowed("overwrite", overwrote_existing=False) is True  # fresh-id overwrite
    assert _compensation_allowed("overwrite", overwrote_existing=True) is False  # replaced a table
    assert _compensation_allowed("exist_ok", overwrote_existing=False) is False
    assert _compensation_allowed("ExistOk", overwrote_existing=False) is False


# --------------------------------------------------------------------------- #
# spec 0.9: describe-at-tag — resolved by the CATALOG (the native backend silently ignores `tag`)
# --------------------------------------------------------------------------- #


def test_describe_at_tag_resolves_via_the_catalog(moto_client: TestClient) -> None:
    """The native dir backend at pylance 8.0.0 IGNORES a describe `tag` (probed: a nonexistent tag
    described the LATEST version, no error) — so the catalog must resolve tag→version itself. Pins:
    tag describes the TAGGED version, unknown tag 404s (never silently the latest), tag+version 400s
    (spec: mutually exclusive)."""
    rows = pa.table({"id": pa.array([1], pa.int64())})
    assert _create(moto_client, "dtag$t", rows).status_code == 200
    assert (
        moto_client.post(
            "/v1/table/dtag$t/insert?mode=append",
            content=_ipc(pa.table({"id": pa.array([2], pa.int64())})),
            headers=ARROW,
        ).status_code
        == 200
    )  # → v2 on disk; v1 is history
    assert moto_client.post("/v1/table/dtag$t/tags/create", json={"tag": "stable", "version": 1}).status_code == 200

    tagged = moto_client.post("/v1/table/dtag$t/describe?tag=stable&load_detailed_metadata=true", json={})
    assert tagged.status_code == 200, tagged.text
    assert tagged.json()["version"] == 1  # the TAGGED version, not the latest
    latest = moto_client.post("/v1/table/dtag$t/describe?load_detailed_metadata=true", json={})
    assert latest.json()["version"] == 2  # untagged describe unchanged

    missing = moto_client.post("/v1/table/dtag$t/describe?tag=nope", json={})
    assert missing.status_code == 404, missing.text  # unknown tag is an ERROR, never silently latest
    both = moto_client.post("/v1/table/dtag$t/describe?tag=stable&version=2", json={})
    assert both.status_code == 400, both.text  # spec 0.9: tag and version are mutually exclusive


# --------------------------------------------------------------------------- #
# §4: every create door refuses a table whose namespace does not exist (#118)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("door", "post"),
    [
        ("arrow-create", lambda c: c.post("/v1/table/ghostns$t9/create", content=_ipc(pa.table({"id": [1]})), headers=ARROW)),
        ("declare", lambda c: c.post("/v1/table/ghostns$t9/declare", json={"schema": {"fields": []}})),
        ("register", lambda c: c.post("/v1/table/ghostns$t9/register", json={"location": "s3://lance-catalog/nope.lance"})),
    ],
)
def test_no_create_door_admits_a_table_whose_NAMESPACE_does_not_exist(moto_client: TestClient, door: str, post: Any) -> None:
    """Parametrized over the doors ON PURPOSE. The guard existed for three of four and the Arrow one
    wrote real datasets into namespaces that were never created (#118) — a hole that survived because
    the guard's tests drove the helper directly and never a door. These drive the HTTP surface, so
    deleting the call from any single endpoint reds exactly one case.
    """
    refused = post(moto_client)
    assert refused.status_code == 404, f"{door} admitted a parentless table: {refused.text}"
    assert "ghostns" in refused.text, f"{door}'s refusal does not name the missing namespace"
    # …and nothing was written: the table is not there to describe, nor listed under the ghost.
    assert moto_client.post("/v1/table/ghostns$t9/describe", json={}).status_code == 404


def test_the_rename_DESTINATION_must_have_a_real_namespace_too(moto_client: TestClient) -> None:
    """Rename is a create at the destination — moving a table into a namespace that does not exist
    orphans it exactly as creating it there would."""
    rows = pa.table({"id": pa.array([1], pa.int64())})
    assert _create(moto_client, "rn$t", rows).status_code == 200

    moved = moto_client.post("/v1/table/rn$t/rename", json={"new_table_name": "t", "new_namespace_id": ["ghostdest"]})
    assert moved.status_code == 404, moved.text
    assert "ghostdest" in moved.text
    assert moto_client.post("/v1/table/rn$t/describe", json={}).status_code == 200, "the source was lost"
