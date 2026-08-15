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


def _client(moto_endpoint: str, monkeypatch: pytest.MonkeyPatch, **extra: str) -> Iterator[TestClient]:
    """The catalog app against moto, with `extra` overriding/adding env before settings are read."""
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{BUCKET}",
        "LANCE_S3_ENDPOINT": moto_endpoint,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_S3_ALLOW_HTTP": "true",
        "LANCE_OIDC_ENABLED": "false",
        "LANCE_FGA_ENABLED": "false",
        **extra,
    }.items():
        monkeypatch.setenv(key, value)  # auto-restored on teardown -> order-independent

    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


@pytest.fixture
def moto_client(moto_endpoint: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    yield from _client(moto_endpoint, monkeypatch)


@pytest.fixture
def moto_client_recoverable(moto_endpoint: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """`moto_client` with a GRACE PERIOD, so `drop` files a trash record instead of destroying.

    A separate fixture rather than a flag on the shared one: `trash_grace_days` defaults to 0 in code
    (`core/config.py`) and a grace period changes what `drop_table` MEANS for every caller, so the
    tests asserting destructive drops must keep the default. Turning it on per-test is also how a real
    deployment turns the feature on — the chart ships 7.
    """
    yield from _client(moto_endpoint, monkeypatch, LANCE_TRASH_GRACE_DAYS="7")


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
# diff2 F3: RETRY CONVERGENCE — every create door, not just the Arrow one
#
# The rule each test below asserts is the same one, and it is deliberately loose about WHICH way a
# door converges: after a failed grant, attempt 2 must leave the object USABLE or leave it GONE.
# Half-states are the defect. An object that exists natively but holds no tuples is invisible to
# every list (per-item FGA filtering), undroppable by everyone (`can_drop: owner`, and `owner from
# parent` cannot help because the seed writes the owner grant and the parent edge in ONE batch — so
# with neither written there is no inheritance path for any admin either), and permanently blocks
# its own id against the retry that would repair it.
#
# `seed_ownership` is patched, NOT `seed_ownership_or_compensate`: the compensating seam is the code
# under test, so stubbing it would assert nothing. The patch lands on the fga_deps module global,
# which is exactly what the seam's internal call resolves.
# --------------------------------------------------------------------------- #


def _failing_seed_ctx(mp: pytest.MonkeyPatch) -> None:
    """Patch the grant to fail the way a real OpenFGA outage does (503 → ServiceUnavailableError)."""
    from lance_namespace import ServiceUnavailableError

    async def failing_seed(*_a: object, **_kw: object) -> None:
        raise ServiceUnavailableError("fga down mid-create")

    import catalog.api.fga_deps as fga_deps_mod

    mp.setattr(fga_deps_mod, "seed_ownership", failing_seed)


def test_declare_converges_after_a_failed_grant(moto_client: TestClient) -> None:
    """DECLARE was bare native-then-seed. A failed grant left a declared-only table that its declarer
    could not see, could not drop and could not re-declare (native `TableAlreadyExists`) — reserving
    the id against everyone, permanently. The undo is unconditionally safe here: a declared table
    holds no data, so nothing can be destroyed by removing it."""
    assert moto_client.post("/v1/namespace/f3d/create", json={}).status_code == 200
    with pytest.MonkeyPatch.context() as mp:
        _failing_seed_ctx(mp)
        failed = moto_client.post("/v1/table/f3d$t/declare", json={})
    assert failed.status_code == 503, failed.text
    # GONE, not stranded — so the id is free…
    assert moto_client.post("/v1/table/f3d$t/describe", json={}).status_code == 404
    # …and the plain retry converges with FGA recovered.
    assert moto_client.post("/v1/table/f3d$t/declare", json={}).status_code == 200


def test_namespace_create_converges_after_a_failed_grant(moto_client: TestClient) -> None:
    """A stranded namespace is worse than a stranded table: it blocks the NAME, and every child
    create under it inherits the missing parent edge."""
    with pytest.MonkeyPatch.context() as mp:
        _failing_seed_ctx(mp)
        failed = moto_client.post("/v1/namespace/f3ns/create", json={})
    assert failed.status_code == 503, failed.text
    assert moto_client.post("/v1/namespace/f3ns/describe", json={}).status_code == 404
    assert moto_client.post("/v1/namespace/f3ns/create", json={}).status_code == 200


def test_undrop_keeps_the_table_recoverable_when_the_grant_fails(moto_client_recoverable: TestClient) -> None:
    """The nastiest of the three, because the RECOVERY door destroyed the means of recovery.

    `trash.clear` ran BEFORE the seed, so a failed grant left the table re-registered but ownerless
    AND its trash record already deleted — simultaneously unreachable and unrecoverable, with no
    path back for anyone. The fix is an ordering one: seed first, clear only once everything that can
    still fail has succeeded. So the assertion that matters is not that undrop failed, it is that a
    SECOND undrop still works.
    """
    rows = pa.table({"id": pa.array([1], pa.int64())})
    assert moto_client_recoverable.post("/v1/namespace/f3u/create", json={}).status_code == 200
    assert _create(moto_client_recoverable, "f3u$t", rows).status_code == 200
    # Trash it (a recoverable drop files the record the undrop below reads).
    dropped = moto_client_recoverable.post("/v1/table/f3u$t/drop", json={})
    assert dropped.status_code == 200, dropped.text

    with pytest.MonkeyPatch.context() as mp:
        _failing_seed_ctx(mp)
        failed = moto_client_recoverable.post("/v1/table/f3u$t/undrop", json={})
    assert failed.status_code == 503, failed.text

    # THE POINT: the trash record survived the failed undrop, so recovery is still possible.
    retried = moto_client_recoverable.post("/v1/table/f3u$t/undrop", json={})
    assert retried.status_code == 200, retried.text
    assert int(moto_client_recoverable.post("/v1/table/f3u$t/count_rows", json={}).text) == 1


def test_register_undo_deregisters_and_never_deletes_the_bytes(moto_client: TestClient) -> None:
    """REGISTER attaches bytes that already existed and are not ours to destroy, so its undo is
    `deregister`, never `drop`. This is the test that would catch someone "simplifying" the four
    undo callbacks into one shared drop: the retry must converge AND the data must survive."""
    rows = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
    assert moto_client.post("/v1/namespace/f3r/create", json={}).status_code == 200
    assert _create(moto_client, "f3r$src", rows).status_code == 200
    described = moto_client.post("/v1/table/f3r$src/describe", json={})
    assert described.status_code == 200, described.text
    location = str(described.json()["location"])
    relative = location.rstrip("/").rsplit("/", 1)[-1]

    with pytest.MonkeyPatch.context() as mp:
        _failing_seed_ctx(mp)
        failed = moto_client.post("/v1/table/f3r$copy/register", json={"location": relative})
    assert failed.status_code == 503, failed.text
    # The catalog object this request made is gone…
    assert moto_client.post("/v1/table/f3r$copy/describe", json={}).status_code == 404
    # …but the underlying data was NOT deleted — the source still reads its three rows.
    assert int(moto_client.post("/v1/table/f3r$src/count_rows", json={}).text) == 3
    # …and the retry converges.
    assert moto_client.post("/v1/table/f3r$copy/register", json={"location": relative}).status_code == 200


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


# --------------------------------------------------------------------------- #
# diff2 F10 item 4 — the trash-window privilege bleed
#
# A recoverable drop deliberately KEEPS the table's FGA tuples: `drop_table` revokes only
# `if not trashed`, because the owner is the one person who needs them to undrop, and revoking made
# undrop unreachable for exactly that caller. Correct for undrop — a hole for create. Nothing read
# the trash on the way in, so a create at the same id during the grace window produced a brand-new
# table silently wearing the DEAD table's grants: every reader, writer and validator it ever had, on
# data they were never granted.
#
# Refusing is the honest answer rather than revoking-then-creating. The id is not free yet: the bytes
# are still on storage and the owner still holds a live claim to recover them, so a create that
# quietly took the name would trade a privilege bleed for silent data loss.
# --------------------------------------------------------------------------- #


def test_create_is_refused_while_the_id_is_still_recoverable(moto_client_recoverable: TestClient) -> None:
    """The window: dropped-but-recoverable, so the old grants are still live."""
    rows = pa.table({"id": pa.array([1], pa.int64())})
    assert moto_client_recoverable.post("/v1/namespace/bleed/create", json={}).status_code == 200
    assert _create(moto_client_recoverable, "bleed$t", rows).status_code == 200
    assert moto_client_recoverable.post("/v1/table/bleed$t/drop", json={}).status_code == 200

    refused = _create(moto_client_recoverable, "bleed$t", rows)
    assert refused.status_code == 409, refused.text
    body = refused.json()
    # The refusal has to be ACTIONABLE: "already exists" about a table the caller cannot see is a
    # dead end. It names the deadline and both ways out.
    assert "recoverable" in body["detail"]
    assert "undrop" in body["detail"]
    assert "purge" in body["detail"]

    # …and the recoverable table is untouched by the refused create — the whole point of refusing
    # rather than revoking-and-overwriting.
    undropped = moto_client_recoverable.post("/v1/table/bleed$t/undrop", json={})
    assert undropped.status_code == 200, undropped.text
    assert int(moto_client_recoverable.post("/v1/table/bleed$t/count_rows", json={}).text) == 1


def test_declare_and_register_are_refused_on_a_trashed_id_too(moto_client_recoverable: TestClient) -> None:
    """All the create doors, not just the Arrow one. A guard on one door is a guard on no door —
    `declare` reserves the same id and `register` attaches bytes to it, and either would inherit the
    dead table's grants exactly as the Arrow create would."""
    rows = pa.table({"id": pa.array([1], pa.int64())})
    assert moto_client_recoverable.post("/v1/namespace/bleed2/create", json={}).status_code == 200
    assert _create(moto_client_recoverable, "bleed2$t", rows).status_code == 200
    described = moto_client_recoverable.post("/v1/table/bleed2$t/describe", json={})
    relative = str(described.json()["location"]).rstrip("/").rsplit("/", 1)[-1]
    assert moto_client_recoverable.post("/v1/table/bleed2$t/drop", json={}).status_code == 200

    assert moto_client_recoverable.post("/v1/table/bleed2$t/declare", json={}).status_code == 409
    assert moto_client_recoverable.post("/v1/table/bleed2$t/register", json={"location": relative}).status_code == 409


def test_a_destructive_drop_frees_the_id_immediately(moto_client: TestClient) -> None:
    """The guard must not outstay its reason. A DESTRUCTIVE drop (the code default, grace=0) revokes
    the tuples with the table, so there is no bleed to prevent and no recovery to protect — the id is
    genuinely free and a create must succeed. Without this, the fix would read as 'creates sometimes
    409 forever' to anyone who did not know the grace period was involved."""
    rows = pa.table({"id": pa.array([1], pa.int64())})
    assert _create(moto_client, "nobleed$t", rows).status_code == 200
    assert moto_client.post("/v1/table/nobleed$t/drop", json={}).status_code == 200
    assert _create(moto_client, "nobleed$t", rows).status_code == 200


# --------------------------------------------------------------------------- #
# diff2 F10 item 5 — the deadline is PURGE-ELIGIBILITY, not the end of recovery
#
# `undrop` never compared `expires_at` to the clock; it checks whether the RECORD is still there.
# That behaviour is right and stays: the bytes are on storage until the purge collects them, so
# refusing would destroy recoverable data on a timestamp alone — and the maintenance plane already
# reasons exactly this way ("a record that survives is a recovery that still works; a purged one is
# not"). What was wrong was the DESCRIPTION: `/tasks` reported the deadline as finality and undrop's
# 404 called an expired drop "genuinely unrecoverable" alongside a never-trashed one.
# --------------------------------------------------------------------------- #


def _expire_trash_record(moto_endpoint: str, canonical: str) -> None:
    """Rewrite ONE table's trash deadline into the past — the state a LATE PURGE leaves behind.

    Reaches into the object store rather than a local path: the control root here is the moto bucket.
    The key comes from `trash._key`, the module's OWN deriver, rather than from listing the prefix:
    `moto_endpoint` is module-scoped, so earlier tests in this file leave their own records in the
    bucket and "the only record" is not a thing that exists. Using the real deriver also means a
    change to the hashing scheme moves this test with it instead of silently mistargeting.
    """
    import json

    from service_kit.lakehouse import trash

    s3 = boto3.client(
        "s3",
        endpoint_url=moto_endpoint,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )
    key = trash._key(canonical)  # already the full `_trash/<kind>-<hash>.json` path
    record = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    record["expires_at"] = "2000-01-01T00:00:00+00:00"
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(record).encode())


def test_an_expired_but_unpurged_drop_still_undrops(moto_client_recoverable: TestClient, moto_endpoint: str) -> None:
    """The behaviour that must NOT change. Past the deadline, before the purge: the bytes are there,
    so recovery works. Refusing here would be destroying data by clock."""
    rows = pa.table({"id": pa.array([1, 2], pa.int64())})
    assert moto_client_recoverable.post("/v1/namespace/exp/create", json={}).status_code == 200
    assert _create(moto_client_recoverable, "exp$t", rows).status_code == 200
    assert moto_client_recoverable.post("/v1/table/exp$t/drop", json={}).status_code == 200

    _expire_trash_record(moto_endpoint, "exp$t")

    # /tasks now SAYS it is expired rather than implying it is gone…
    tasks = moto_client_recoverable.get("/v1/table/exp$t/tasks").json()
    assert tasks and tasks[0]["expired"] is True

    # …and the undrop still works, with the rows intact.
    undropped = moto_client_recoverable.post("/v1/table/exp$t/undrop", json={})
    assert undropped.status_code == 200, undropped.text
    assert int(moto_client_recoverable.post("/v1/table/exp$t/count_rows", json={}).text) == 2


def test_a_live_deadline_is_not_reported_as_expired(moto_client_recoverable: TestClient) -> None:
    """The flag must distinguish 'you have a week' from 'go now', or it is noise."""
    rows = pa.table({"id": pa.array([1], pa.int64())})
    assert moto_client_recoverable.post("/v1/namespace/live/create", json={}).status_code == 200
    assert _create(moto_client_recoverable, "live$t", rows).status_code == 200
    assert moto_client_recoverable.post("/v1/table/live$t/drop", json={}).status_code == 200

    tasks = moto_client_recoverable.get("/v1/table/live$t/tasks").json()
    assert tasks and tasks[0]["expired"] is False
    assert tasks[0]["expires_at"]


def test_an_unparseable_deadline_is_not_reported_as_expired(moto_client_recoverable: TestClient) -> None:
    """Fail toward 'still recoverable'. A malformed timestamp must not make a table that is still
    on storage read as beyond saving — the flag adds urgency, it never withdraws hope."""
    from catalog.api.v1.endpoints.tables import _is_expired

    assert _is_expired("") is False
    assert _is_expired("not-a-date") is False
    assert _is_expired("2000-01-01T00:00:00+00:00") is True
    # A naive stamp (records written before the offset was included) is read as UTC, not rejected.
    assert _is_expired("2000-01-01T00:00:00") is True
