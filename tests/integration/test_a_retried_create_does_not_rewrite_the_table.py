"""A create replayed with the same `Idempotency-Key` answers once and executes once.

§ Q17-32. A retrying client replays a write door's failure, and a replay of `POST /v1/table/{id}/create`
that carries no key re-enters `create_governed_table`: under `mode=Overwrite` that DROPS AND REWRITES the
dataset, strips and re-seeds the ACL, and emits a second `table_created`; under `mode=Create` it answers
AlreadyExists, so a fully successful create surfaces as a 409 nobody can tell from a name collision.

THE KEY IS OPTIONAL AND THE FIRST TEST HERE IS THAT IT STAYS SO. The Lance Namespace spec defines no
such header and a stock client must work with no rask SDK, so a door that required it would fail
conformance on 54 routed operations — a worse defect than the one being fixed.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def _rows(n: int = 3) -> bytes:
    return _ipc(pa.table({"id": pa.array(range(n), pa.int64())}))


def _versions(client: TestClient, tid: str) -> Any:
    return client.post(f"/v1/table/{tid}/describe", json={}).json()


def _version(client: TestClient, tid: str) -> int:
    return int(client.post(f"/v1/table/{tid}/describe", json={"load_detailed_metadata": True}).json()["version"])


def _keyed_post_paths(app: Any) -> tuple[set[str], set[str]]:
    """`(paths declaring Idempotency-Key, paths REQUIRING it)`, read off the generated OpenAPI.

    NOT off `app.routes`: the catalog mounts its versioned routers, so a walk over that finds ONE
    APIRoute and a roster gate built on it would pass by finding nothing — which is precisely the
    failure a roster gate exists to prevent. `app.openapi()` is the same document a stock Lance client
    reads, which also makes this the right surface for a claim about spec conformance.
    """
    spec = app.openapi()
    declared: set[str] = set()
    required: set[str] = set()
    for path, ops in spec.get("paths", {}).items():
        for param in (ops.get("post") or {}).get("parameters", []):
            if param.get("in") == "header" and param.get("name") == "Idempotency-Key":
                declared.add(path)
                if param.get("required"):
                    required.add(path)
    return declared, required


class TestAReplayIsAnsweredNotReExecuted:
    @pytest.mark.parametrize(
        ("query", "rows", "answered"),
        [pytest.param("", 3, (200, None), id="the-same-request"), pytest.param("?mode=Overwrite", 99, (400, 13), id="another-request")],
    )
    def test_the_same_key_never_runs_the_create_again(self, real_ns_client: TestClient, query: str, rows: int, answered: tuple[int, int | None]) -> None:
        """The second call under the key must not re-enter the create path at all. The same request is answered with the
        first attempt's response; another one under the key (other bytes, another mode) is refused 400, since an answer
        of the first would say a create the caller did not ask for is what happened (ADR 0240, KeyBindsBody), and
        honouring its body would let a retry silently overwrite the table with something else."""
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        headers = {**ARROW_STREAM, "Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f60"}

        first = real_ns_client.post("/v1/table/lance_catalog$db$keyed/create", content=_rows(3), headers=headers)
        assert first.status_code == 200, first.text
        after_first = _versions(real_ns_client, "lance_catalog$db$keyed")

        second = real_ns_client.post(f"/v1/table/lance_catalog$db$keyed/create{query}", content=_rows(rows), headers=headers)

        assert (second.status_code, second.json().get("code")) == answered, second.text
        if second.status_code == 200:
            assert second.json() == first.json(), "a replay must answer with the first attempt's response"
        assert _versions(real_ns_client, "lance_catalog$db$keyed") == after_first, "the second call re-executed the create"

    def test_a_DIFFERENT_key_is_a_different_request(self, real_ns_client: TestClient) -> None:
        """Convergence must not become deduplication of unrelated calls."""
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        first = real_ns_client.post(
            "/v1/table/lance_catalog$db$two/create", content=_rows(), headers={**ARROW_STREAM, "Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f61"}
        )
        assert first.status_code == 200, first.text

        second = real_ns_client.post(
            "/v1/table/lance_catalog$db$two/create?mode=Overwrite",
            content=_rows(5),
            headers={**ARROW_STREAM, "Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f62"},
        )

        assert second.status_code == 200, second.text


class TestTheHeaderIsConstrainedAtTheDoor:
    @pytest.mark.parametrize("bad", ["..", "retry-1"])
    def test_a_key_that_is_not_a_uuid_is_refused(self, real_ns_client: TestClient, bad: str) -> None:
        """It becomes part of an object key, so it is a UUID, a segment no spelling turns into a path. Refused at the
        header AND at the seam: the door's refusal is the better error, and the seam's `ValueError` is what protects
        a second caller of the module. `..` once reached the seam and answered 500.

        400 because `CreateTable` is a SPEC operation: the vendored spec has no 422 and maps
        `InvalidInput` to 400. It also makes this door answer one way — `TestTheKeyIsScopedToItsEndpoint`
        above already expects 400 for a CROSSED key, so a malformed one answering 422 had the same door
        giving two statuses for two flavours of the same invalid input.
        """
        response = real_ns_client.post("/v1/table/lance_catalog$db$y/create", content=_rows(), headers={**ARROW_STREAM, "Idempotency-Key": bad})

        assert (response.status_code, response.json().get("code")) == (400, 13), response.text


class TestTheDestructiveDoorsConvergeToo:
    """`drop` and `rename` carry the same amplifier as `create` and a worse consequence.

    A replayed drop re-enters a door that DELETES BYTES (or files a second trash record for an object
    already gone). A replayed rename re-enters one that retires the source id and re-registers the
    destination — and the second pass finds no source, so a rename that SUCCEEDED reports as a failure
    the caller cannot distinguish from a name that never existed.
    """

    def test_a_replayed_drop_answers_once_and_the_second_call_does_not_re_enter(self, real_ns_client: TestClient) -> None:
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/lance_catalog$db$doomed/create", content=_rows(), headers=ARROW_STREAM).status_code == 200
        headers = {"Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f63"}

        first = real_ns_client.post("/v1/table/lance_catalog$db$doomed/drop", headers=headers)
        assert first.status_code == 200, first.text

        # Without convergence this re-enters the door and answers NotFound — a successful drop
        # reported as a failure, which is the create door's 409 problem wearing another status.
        second = real_ns_client.post("/v1/table/lance_catalog$db$doomed/drop", headers=headers)

        assert second.status_code == 200, second.text
        assert second.json() == first.json()

    def test_a_replayed_rename_does_not_report_the_success_as_a_missing_source(self, real_ns_client: TestClient) -> None:
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/lance_catalog$db$before/create", content=_rows(), headers=ARROW_STREAM).status_code == 200
        headers = {"Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f64"}
        body = {"id": ["lance_catalog", "db", "before"], "new_table_name": "after"}

        first = real_ns_client.post("/v1/table/lance_catalog$db$before/rename", json=body, headers=headers)
        assert first.status_code == 200, first.text

        second = real_ns_client.post("/v1/table/lance_catalog$db$before/rename", json=body, headers=headers)

        assert second.status_code == 200, second.text
        assert real_ns_client.post("/v1/table/lance_catalog$db$after/describe", json={}).status_code == 200, "the destination must still be there"

    def test_each_door_owns_its_key_namespace(self, real_ns_client: TestClient) -> None:
        """One key across create and drop is the cross-endpoint refusal, not a replay — proven here at
        the doors rather than only at the seam, because the endpoint string is what binds them and a
        typo in one would silently merge two operations' keys."""
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert (
            real_ns_client.post(
                "/v1/table/lance_catalog$db$shared/create", content=_rows(), headers={**ARROW_STREAM, "Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f65"}
            ).status_code
            == 200
        )

        crossed = real_ns_client.post("/v1/table/lance_catalog$db$shared/drop", headers={"Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f65"})

        assert crossed.status_code == 400, crossed.text

    def test_a_key_reused_on_another_table_is_refused_and_the_table_is_left(self, real_ns_client: TestClient) -> None:
        """[[LH-576]] A key binds the canonical id of the object its door acts on as well as the door: answered with the
        first table's drop, a second drop under the key would report success and leave the second table standing."""
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        for table in ("lance_catalog$db$one", "lance_catalog$db$other"):
            assert real_ns_client.post(f"/v1/table/{table}/create", content=_rows(), headers=ARROW_STREAM).status_code == 200
        keyed = {"Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f66"}
        assert real_ns_client.post("/v1/table/lance_catalog$db$one/drop", headers=keyed).status_code == 200

        reused = real_ns_client.post("/v1/table/lance_catalog$db$other/drop", headers=keyed)

        assert (reused.status_code, reused.headers["content-type"], reused.json().get("code")) == (400, "application/problem+json", 13), reused.text
        assert real_ns_client.post("/v1/table/lance_catalog$db$other/describe", json={}).status_code == 200, "the refused drop must leave the table"


class TestARetriedAppendLandsOnce:
    """[[LH-577]] An append is the write a caller most often gives up on and sends again: the console aborts a slow one,
    and the catalog may have committed it. Under one key the second send answers the first's response and appends
    nothing; one carrying other rows is refused 400 and appends nothing either (ADR 0240, KeyBindsBody); a key the append
    has not used is another append."""

    @pytest.mark.parametrize(("rows", "answered"), [pytest.param(2, 200, id="the-same-rows"), pytest.param(5, 400, id="other-rows")])
    def test_the_same_key_appends_once_and_answers_the_first_response(self, real_ns_client: TestClient, rows: int, answered: int) -> None:
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/lance_catalog$db$grown/create", content=_rows(3), headers=ARROW_STREAM).status_code == 200
        before = _version(real_ns_client, "lance_catalog$db$grown")
        keyed = {**ARROW_STREAM, "Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f67"}

        first = real_ns_client.post("/v1/table/lance_catalog$db$grown/insert?mode=append", content=_rows(2), headers=keyed)
        assert first.status_code == 200, first.text
        again = real_ns_client.post("/v1/table/lance_catalog$db$grown/insert?mode=append", content=_rows(rows), headers=keyed)
        other = real_ns_client.post(
            "/v1/table/lance_catalog$db$grown/insert?mode=append",
            content=_rows(1),
            headers={**ARROW_STREAM, "Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f68"},
        )

        assert (again.status_code, again.json() if answered == 200 else again.json().get("code")) == (answered, first.json() if answered == 200 else 13), (
            again.text
        )
        assert other.status_code == 200, other.text
        counted = real_ns_client.post("/v1/table/lance_catalog$db$grown/count_rows", json={})
        assert (_version(real_ns_client, "lance_catalog$db$grown"), counted.text) == (before + 2, "6"), "a replayed append appended again"

    @pytest.mark.parametrize(
        "after",
        ["one-takeover", "a-chain-of-takeovers", "a-takeover-refused-before-its-search", "its-version-deleted-since", "its-version-deleted-as-it-is-searched"],
    )
    @pytest.mark.parametrize("door", ["insert-no-key", "insert-primary-key", "create-overwrite", "restore"])
    def test_a_write_whose_answer_was_lost_past_the_lease_is_found_in_its_commit(
        self, real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch, door: str, after: str
    ) -> None:
        """The answer of a keyed write can be lost after it committed (its pod ended, or a fault after the commit), and a
        resend past the claim's lease takes the key over. Run again, the write would land twice: its commit carries the
        key, so the takeover finds it there and answers with it (ADR 0240), at every door that lands a version: an append
        answers the rows it added, whatever operation committed them (an append to a table declaring a primary key commits
        as Lance's merge, an ``Update``, ADR 0240 TakeoverCounts), a create the version, a restore the transaction.

        However many attempts came between (ADR 0240 §11): ``a-chain-of-takeovers``, the takeover that found the write lost
        its answer too, and a third send takes the key over from it (KeyKeepsFirst: the search starts at the first put the
        key's record kept, where it started at that takeover's own claim and missed the write);
        ``a-takeover-refused-before-its-search``, the takeover was refused 409 before it searched, which gave the key back,
        and a third send claims the released key (ReleaseKeepsWritten: a released key whose record says a put may have
        landed is searched, where it was taken as unwritten and the write landed again).

        ``its-version-deleted-since`` (ADR 0240 §12, SearchWhole): another write lands after it and the owner deletes the
        version the write landed as (``version/delete``), inside the key's lifetime. The search cannot read that version,
        so it cannot prove the write did not land: the resend is refused 409 InvalidTableState (19) and writes nothing,
        where it found no stamp and landed the write a second time. ``its-version-deleted-as-it-is-searched``: the delete
        lands between the listing of the window and the read of that version, so the listing still names it; the read fails,
        the listing read again says the version is gone, and the resend is refused the same way, where the read's failure
        answered as the store's."""
        from lance_namespace import ConcurrentModificationError

        from catalog.api import idempotency as keyed_doors
        from catalog.services import dataplane
        from lakehouse import idempotency

        metadata = {"lance-schema:unenforced-primary-key": "true"} if door == "insert-primary-key" else None
        schema = pa.schema([pa.field("id", pa.int64(), nullable=False, metadata=metadata)])

        def rows(*ids: int) -> bytes:
            return _ipc(pa.table({"id": pa.array(ids, pa.int64())}, schema=schema))

        table = "/v1/table/lance_catalog$db$lost"
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert real_ns_client.post(f"{table}/create", content=rows(0, 1, 2), headers=ARROW_STREAM).status_code == 200
        before = _version(real_ns_client, "lance_catalog$db$lost")
        keyed = {"Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f69"}

        def send() -> httpx.Response:
            if door == "create-overwrite":
                return real_ns_client.post(f"{table}/create?mode=Overwrite", content=rows(7, 8), headers={**ARROW_STREAM, **keyed})
            if door == "restore":
                return real_ns_client.post(f"{table}/restore", json={"version": before}, headers=keyed)
            return real_ns_client.post(f"{table}/insert?mode=append", content=rows(7, 8), headers={**ARROW_STREAM, **keyed})

        async def lost(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("the pod ended before the answer was recorded")

        def busy(*_args: object, **_kwargs: object) -> None:
            raise ConcurrentModificationError("the table changed while the request was served; retry")

        with monkeypatch.context() as during:
            during.setattr(keyed_doors.Converge, "remember", lost)
            with pytest.raises(RuntimeError, match="the pod ended"):
                send()
            landed = real_ns_client.post(f"{table}/describe", json={"load_detailed_metadata": True}).json()
            during.setattr(keyed_doors.idempotency, "claim", partial(idempotency.claim, lease=timedelta(0)))
            if after == "a-chain-of-takeovers":
                with pytest.raises(RuntimeError, match="the pod ended"):
                    send()
            elif after == "a-takeover-refused-before-its-search":
                during.setattr(dataplane, "landed_under_key", busy)
                refused = send()
                assert (refused.status_code, refused.json().get("code")) == (409, 14), refused.text
        if after.startswith("its-version-deleted"):
            assert real_ns_client.post(f"{table}/insert?mode=append", content=rows(9), headers=ARROW_STREAM).status_code == 200
            deleted = real_ns_client.post(f"{table}/version/delete", json={"ranges": [{"start_version": before + 1, "end_version": before + 2}]})
            assert (deleted.status_code, deleted.json().get("deleted_count")) == (200, 1), deleted.text
        if after == "its-version-deleted-as-it-is-searched":
            listings = iter([{*range(1, before + 3)}])
            relisted = dataplane._listed_versions
            monkeypatch.setattr(dataplane, "_listed_versions", lambda ref_root, so: next(listings, None) or relisted(ref_root, so))
        monkeypatch.setattr(keyed_doors.idempotency, "claim", partial(idempotency.claim, lease=timedelta(0)))
        again = send()

        if after.startswith("its-version-deleted"):
            assert (again.status_code, again.json().get("code")) == (409, 19), again.text
            assert f"version {before + 1} of the table" in again.json()["detail"], again.text
            assert _version(real_ns_client, "lance_catalog$db$lost") == before + 2, "the write ran again after its version was deleted"
            return
        assert again.status_code == 200, again.text
        if door.startswith("insert"):
            assert again.json() == {"num_inserted_rows": 2, "version": before + 1}
        elif door == "create-overwrite":
            assert again.json() == {"location": landed["location"], "version": before + 1}
        counted = real_ns_client.post(f"{table}/count_rows", json={})
        held = {"create-overwrite": "2", "restore": "3"}.get(door, "5")
        assert (_version(real_ns_client, "lance_catalog$db$lost"), counted.text) == (before + 1, held), "the write ran again after its lease"

    def test_the_key_names_one_append_on_one_ref(self, real_ns_client: TestClient) -> None:
        """The same key on another mode or another ref is another operation, refused rather than answered with this one."""
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/lance_catalog$db$moded/create", content=_rows(3), headers=ARROW_STREAM).status_code == 200
        keyed = {**ARROW_STREAM, "Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f6a"}
        assert real_ns_client.post("/v1/table/lance_catalog$db$moded/insert?mode=append", content=_rows(2), headers=keyed).status_code == 200

        crossed = real_ns_client.post("/v1/table/lance_catalog$db$moded/insert?mode=overwrite", content=_rows(1), headers=keyed)

        assert (crossed.status_code, crossed.json().get("code")) == (400, 13), crossed.text
        assert real_ns_client.post("/v1/table/lance_catalog$db$moded/count_rows", json={}).text == "5"


class TestEveryTableDoorThatMintsOrRetiresConverges:
    """`create`/`drop`/`rename` were the first three; these are the rest of the same amplifier.

    Pinned as a ROSTER rather than one test each, because the failure this guards is a door ADDED later
    without the key — and a per-door test cannot notice a door nobody wrote a test for.
    """

    def test_the_full_roster_declares_the_header(self, real_ns_client: TestClient) -> None:
        want = {
            "/v1/table/{id}/create",
            "/v1/table/{id}/drop",
            "/v1/table/{id}/rename",
            "/v1/table/{id}/deregister",
            "/v1/table/{id}/register",
            "/v1/table/{id}/restore",
            "/v1/table/{id}/insert",
        }
        keyed, _ = _keyed_post_paths(real_ns_client.app)

        assert want <= keyed, f"these doors mint or retire an id and take no idempotency key: {sorted(want - keyed)}"
