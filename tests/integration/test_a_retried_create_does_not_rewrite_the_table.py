"""A create replayed with the same `Idempotency-Key` answers once and executes once.

§ Q17-32. A retrying client replays a write door's failure, and a replay of `POST /v1/table/{id}/create`
that carries no key re-enters `create_governed_table`: under `mode=Overwrite` that DROPS AND REWRITES the
dataset, strips and re-seeds the ACL, and emits a second `table_created`; under `mode=Create` it answers
AlreadyExists, so a fully successful create surfaces as a 409 nobody can tell from a name collision.

THE KEY IS OPTIONAL AND THE FIRST TEST HERE IS THAT IT STAYS SO. The Lance Namespace spec defines no
such header and a stock client must work with no rask SDK, so a door that required it would fail
conformance on 54 routed operations — a worse defect than the one being fixed.
"""

from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, NamedTuple

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from catalog.core.run_context import IDEMPOTENCY_ANSWER_PROPERTY
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

ARROW_STREAM = {"content-type": ARROW_STREAM_MEDIA_TYPE}


def _rows(n: int = 3) -> bytes:
    return encode_arrow_stream(pa.table({"id": pa.array(range(n), pa.int64())}))


def _versions(client: TestClient, tid: str) -> Any:
    return client.post(f"/v1/table/{tid}/describe", json={}).json()


def _version(client: TestClient, tid: str) -> int:
    return int(client.post(f"/v1/table/{tid}/describe", json={"load_detailed_metadata": True}).json()["version"])


def _keyed_doors(app: Any) -> tuple[set[str], set[str]]:
    """`(doors declaring Idempotency-Key, doors REQUIRING it)`, each as `METHOD path`, read off the generated OpenAPI.

    NOT off `app.routes`: the catalog mounts its versioned routers, so a walk over that finds ONE
    APIRoute and a roster gate built on it would pass by finding nothing — which is precisely the
    failure a roster gate exists to prevent. `app.openapi()` is the same document a stock Lance client
    reads, which also makes this the right surface for a claim about spec conformance.
    """
    spec = app.openapi()
    declared: set[str] = set()
    required: set[str] = set()
    for path, ops in spec.get("paths", {}).items():
        for method in ("post", "delete"):
            for param in (ops.get(method) or {}).get("parameters", []):
                if param.get("in") == "header" and param.get("name") == "Idempotency-Key":
                    declared.add(f"{method.upper()} {path}")
                    if param.get("required"):
                        required.add(f"{method.upper()} {path}")
    return declared, required


class TestAReplayIsAnsweredNotReExecuted:
    def test_the_same_key_returns_the_first_answer_and_leaves_the_table_alone(self, real_ns_client: TestClient) -> None:
        """The defect this closes: the second call must not re-enter the create path at all."""
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        headers = {**ARROW_STREAM, "Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f60"}

        first = real_ns_client.post("/v1/table/lance_catalog$db$keyed/create", content=_rows(3), headers=headers)
        assert first.status_code == 200, first.text
        after_first = _versions(real_ns_client, "lance_catalog$db$keyed")
        second = real_ns_client.post("/v1/table/lance_catalog$db$keyed/create", content=_rows(3), headers=headers)

        assert second.status_code == 200, second.text
        assert second.json() == first.json(), "a replay must answer with the first attempt's response"
        assert _versions(real_ns_client, "lance_catalog$db$keyed") == after_first, "the replay re-executed the create"

        # A REPLAY CARRYING DIFFERENT BYTES. The key says "this is the same request": honouring the body would let a
        # retry silently overwrite the table with something else, and answering the first create would tell the caller
        # its rows are there when they are not.
        other = real_ns_client.post("/v1/table/lance_catalog$db$keyed/create?mode=Overwrite", content=_rows(99), headers=headers)

        assert (other.status_code, other.json().get("code")) == (400, 13), other.text
        assert _versions(real_ns_client, "lance_catalog$db$keyed") == after_first, "the refused replay re-executed the create"

    def test_a_different_key_is_a_different_request(self, real_ns_client: TestClient) -> None:
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

    def test_a_replayed_cascade_drop_of_a_namespace_answers_once_and_does_not_re_enter(self, real_ns_client: TestClient) -> None:
        """A cascade removes every table beneath the namespace, so its replay finds no namespace: the drop that
        succeeded would answer NamespaceNotFound, and one racing a namespace created since under the name would cascade
        through that one."""
        assert real_ns_client.post("/v1/namespace/lance_catalog$doomed/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/lance_catalog$doomed$t/create", content=_rows(), headers=ARROW_STREAM).status_code == 200
        headers = {"Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f6b"}

        first = real_ns_client.post("/v1/namespace/lance_catalog$doomed/drop", json={"behavior": "Cascade"}, headers=headers)
        assert first.status_code == 200, first.text
        assert real_ns_client.post("/v1/namespace/lance_catalog$doomed/create", json={}).status_code == 200
        second = real_ns_client.post("/v1/namespace/lance_catalog$doomed/drop", json={"behavior": "Cascade"}, headers=headers)

        assert (second.status_code, second.json()) == (200, first.json()), second.text
        assert real_ns_client.post("/v1/namespace/lance_catalog$doomed/describe", json={}).status_code == 200, "the replay dropped the namespace created since"

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


def _frame(ids: list[int], xs: list[int]) -> bytes:
    return encode_arrow_stream(pa.table({"id": pa.array(ids, pa.int64()), "x": pa.array(xs, pa.int64())}))


def _table_rows(client: TestClient, tid: str) -> dict[str, list[int]]:
    """The table's rows, by column, ordered by ``id``."""
    response = client.post(f"/v1/table/{tid}/query", json={"k": 100, "vector": {}})
    assert response.status_code == 200, response.text
    return dict(ipc.open_file(pa.py_buffer(response.content)).read_all().sort_by("id").to_pydict())


class _TableWrite(NamedTuple):
    """One write door a blind retry would apply again: the request it is sent, another request of the door on the same
    table and ref (other bytes), the first request's bytes with other parameters, what it answers on the seed table, and
    what a takeover answers for its commit when the commit carries the key and no answer."""

    path: str
    json: dict[str, Any] | None
    content: bytes | None
    other: dict[str, Any] | bytes
    #: ``(path, headers, JSON bytes)``: the first request's Arrow bytes on another path or with other headers, or, for a
    #: door whose parameters all ride its JSON body (update, delete), the same document spelt with other spacing, which
    #: the rule judges another request as well.
    reworded: tuple[str, dict[str, str], bytes | None]
    answer: dict[str, int]
    unanswered: dict[str, int]

    def send(self, client: TestClient, tid: str, headers: dict[str, str], *, other: bool = False) -> Any:
        if self.content is not None:
            content = self.other if other else self.content
            assert isinstance(content, bytes), "an Arrow door's other request is bytes"
            return client.post(f"/v1/table/{tid}/{self.path}", content=content, headers={**ARROW_STREAM, **headers})
        return client.post(f"/v1/table/{tid}/{self.path}", json=self.other if other else self.json, headers=headers)

    def send_reworded(self, client: TestClient, tid: str, headers: dict[str, str]) -> Any:
        path, extra, document = self.reworded
        if self.content is not None:
            return client.post(f"/v1/table/{tid}/{path}", content=self.content, headers={**ARROW_STREAM, **extra, **headers})
        return client.post(f"/v1/table/{tid}/{path}", content=document, headers={"content-type": "application/json", **extra, **headers})


#: A run the spec Context names: the insert stamps it into its commit, so the same rows under another run are another
#: request.
_RUN = {"x-rask-run-id": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f70", "x-rask-job-namespace": "pipelines", "x-rask-job": "nightly"}

_MERGE = "merge_insert?on=id&when_matched_update_all=true&when_not_matched_by_source_delete=true"

#: Each door applied twice to the seed rows ``id`` 1-3, ``x`` 0, with a writer's row landing in between, ends elsewhere than
#: applied once: the append lands its row again, ``x + 1`` adds again, the merge's delete-not-matched-by-source and the
#: delete each take the writer's row.
_TABLE_WRITES = {
    "insert": _TableWrite(
        "insert?mode=append",
        None,
        _frame([10], [10]),
        _frame([11, 12], [0, 0]),
        ("insert?mode=append", _RUN, None),
        {"num_inserted_rows": 1},
        {"num_inserted_rows": 1},
    ),
    "update": _TableWrite(
        "update",
        {"updates": [["x", "x + 1"]]},
        None,
        {"updates": [["x", "x + 5"]]},
        ("update", {}, b'{ "updates": [ ["x", "x + 1"] ] }'),
        {"updated_rows": 3},
        {"updated_rows": 3},
    ),
    "merge_insert": _TableWrite(
        _MERGE,
        None,
        _frame([1], [100]),
        _frame([2], [200]),
        (f"{_MERGE}&when_not_matched_insert_all=true", {}, None),
        {"num_updated_rows": 1, "num_inserted_rows": 0, "num_deleted_rows": 2},
        {},
    ),
    "delete": _TableWrite("delete", {"predicate": "id >= 2"}, None, {"predicate": "id >= 3"}, ("delete", {}, b'{ "predicate": "id >= 2" }'), {}, {}),
}

#: How the second request is sent: under the first one's key, with no key, or under a fresh key.
_FIRST_KEY = "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f67"
_RESENDS: dict[str, str | None] = {
    "same-key": _FIRST_KEY,
    "keyless": None,
    "fresh-key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f6c",
}


def _seeded(client: TestClient, *names: str) -> None:
    assert client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
    for name in names:
        assert client.post(f"/v1/table/lance_catalog$db${name}/create", content=_frame([1, 2, 3], [0, 0, 0]), headers=ARROW_STREAM).status_code == 200


@pytest.mark.parametrize("door", list(_TABLE_WRITES))
class TestARetriedTableWriteCommitsOnce:
    """[[LH-577]], XC-219. A caller that gave up on a slow write, which may have committed, sends it again. Every door
    whose write a blind retry applies again takes the key: under one key the same request answers the first's response
    and commits nothing, though another writer committed in between; the key sent with another request (other bytes, or
    the same bytes with other parameters) or on another table is refused; a resend with no key, or under a fresh key, is
    applied again."""

    @pytest.mark.parametrize("resend", list(_RESENDS))
    def test_a_resend_under_its_key_answers_the_first_and_commits_nothing(self, real_ns_client: TestClient, door: str, resend: str) -> None:
        write = _TABLE_WRITES[door]
        tid = "lance_catalog$db$t"
        _seeded(real_ns_client, "t")
        before = _version(real_ns_client, tid)
        key = _RESENDS[resend]
        resent_key = {"Idempotency-Key": key} if key is not None else {}
        first_key = {"Idempotency-Key": _FIRST_KEY} if key is not None else {}

        first = write.send(real_ns_client, tid, first_key)
        assert (first.status_code, first.json()) == (200, {**write.answer, "version": before + 1}), first.text
        assert real_ns_client.post(f"/v1/table/{tid}/insert?mode=append", content=_frame([9], [0]), headers=ARROW_STREAM).status_code == 200
        between = (_table_rows(real_ns_client, tid), _version(real_ns_client, tid))
        again = write.send(real_ns_client, tid, resent_key)

        assert again.status_code == 200, again.text
        if resend == "same-key":
            assert again.json() == first.json(), "a resend under its key must answer the first attempt's response"
            assert (_table_rows(real_ns_client, tid), _version(real_ns_client, tid)) == between, f"the resent {door} committed again"
        else:
            assert _version(real_ns_client, tid) == between[1] + 1, "a resend with no key or a fresh one is a new request, applied again"
            assert _table_rows(real_ns_client, tid) != between[0]

    @pytest.mark.parametrize("request_sent", ["other-bytes", "other-parameters"])
    def test_the_key_sent_with_another_request_is_refused_and_commits_nothing(self, real_ns_client: TestClient, door: str, request_sent: str) -> None:
        write = _TABLE_WRITES[door]
        tid = "lance_catalog$db$t"
        _seeded(real_ns_client, "t")
        keyed = {"Idempotency-Key": _FIRST_KEY}
        assert write.send(real_ns_client, tid, keyed).status_code == 200
        assert real_ns_client.post(f"/v1/table/{tid}/insert?mode=append", content=_frame([9], [0]), headers=ARROW_STREAM).status_code == 200
        between = (_table_rows(real_ns_client, tid), _version(real_ns_client, tid))

        if request_sent == "other-bytes":
            refused = write.send(real_ns_client, tid, keyed, other=True)
        else:
            refused = write.send_reworded(real_ns_client, tid, keyed)

        assert (refused.status_code, refused.json().get("code")) == (400, 13), refused.text
        assert "different request" in refused.json()["detail"], refused.text
        assert (_table_rows(real_ns_client, tid), _version(real_ns_client, tid)) == between, f"the refused {door} committed"
        resent = write.send(real_ns_client, tid, keyed)
        assert (resent.status_code, _version(real_ns_client, tid)) == (200, between[1]), "the first request's own resend is still answered"

    def test_the_same_key_on_another_table_is_refused_and_the_table_is_left(self, real_ns_client: TestClient, door: str) -> None:
        write = _TABLE_WRITES[door]
        _seeded(real_ns_client, "t", "other")
        keyed = {"Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f68"}
        assert write.send(real_ns_client, "lance_catalog$db$t", keyed).status_code == 200
        untouched = (_table_rows(real_ns_client, "lance_catalog$db$other"), _version(real_ns_client, "lance_catalog$db$other"))

        crossed = write.send(real_ns_client, "lance_catalog$db$other", keyed)

        assert (crossed.status_code, crossed.json().get("code")) == (400, 13), crossed.text
        assert (_table_rows(real_ns_client, "lance_catalog$db$other"), _version(real_ns_client, "lance_catalog$db$other")) == untouched

    @pytest.mark.parametrize("resent", ["answer-stamped", "key-only", "other-request"])
    def test_a_write_whose_answer_was_lost_past_the_lease_is_found_in_its_commit(
        self, real_ns_client: TestClient, door: str, resent: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The answer of a write can be lost after it committed (its pod ended, or a fault after the commit), and a resend
        past the claim's lease takes the key over. Run again, the write would apply twice: the commit carries the key and
        the answer, so the takeover finds them there and answers with them. A commit carrying the key and no answer (one
        an image that stamped only the key made) is answered from what its transaction holds, never a fault. A takeover
        sent with another request is refused by the claim the lost attempt left, which keeps that attempt's digest."""
        from catalog.api import idempotency as keyed_doors
        from catalog.services import idempotency_records
        from catalog.services.tables import writes

        write = _TABLE_WRITES[door]
        tid = "lance_catalog$db$lost"
        _seeded(real_ns_client, "lost")
        before = _version(real_ns_client, tid)
        keyed = {"Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f69"}

        async def lost(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("the pod ended before the answer was recorded")

        with monkeypatch.context() as during:
            during.setattr(keyed_doors.Converge, "remember", lost)
            if resent == "key-only":
                during.setattr(writes, "_with_answer", lambda properties, _answer: properties)
                during.setattr(writes, "_answering", lambda _properties, plan, _answer: plan)
            with pytest.raises(RuntimeError, match="the pod ended"):
                write.send(real_ns_client, tid, keyed)
        landed = _table_rows(real_ns_client, tid)
        if resent == "key-only":
            lapsed = lance.dataset(_versions(real_ns_client, tid)["location"]).read_transaction(before + 1)
            assert lapsed is not None
            assert (lapsed.transaction_properties or {}).get(IDEMPOTENCY_ANSWER_PROPERTY) is None, "the commit must carry its key alone"
        monkeypatch.setattr(keyed_doors.idempotency_records, "claim", partial(idempotency_records.claim, lease=timedelta(0)))
        again = write.send(real_ns_client, tid, keyed, other=resent == "other-request")

        if resent == "other-request":
            assert (again.status_code, again.json().get("code")) == (400, 13), again.text
        else:
            answered = write.answer if resent == "answer-stamped" else write.unanswered
            assert (again.status_code, again.json()) == (200, {**answered, "version": before + 1}), again.text
        assert (_table_rows(real_ns_client, tid), _version(real_ns_client, tid)) == (landed, before + 1), f"the {door} ran again after its lease"


class TestARetriedAppendNamesItsMode:
    def test_the_key_names_one_append_on_one_ref(self, real_ns_client: TestClient) -> None:
        """The same key on another mode or another ref is another operation, refused rather than answered with this one."""
        assert real_ns_client.post("/v1/namespace/lance_catalog$db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/lance_catalog$db$moded/create", content=_rows(3), headers=ARROW_STREAM).status_code == 200
        keyed = {**ARROW_STREAM, "Idempotency-Key": "6a1c9d5e-0f3b-4c2a-8e7d-1b2c3d4e5f6a"}
        assert real_ns_client.post("/v1/table/lance_catalog$db$moded/insert?mode=append", content=_rows(2), headers=keyed).status_code == 200

        crossed = real_ns_client.post("/v1/table/lance_catalog$db$moded/insert?mode=overwrite", content=_rows(1), headers=keyed)

        assert (crossed.status_code, crossed.json().get("code")) == (400, 13), crossed.text
        assert real_ns_client.post("/v1/table/lance_catalog$db$moded/count_rows", json={}).text == "5"


class TestEveryDoorARetryWouldApplyAgainConverges:
    """The roster of the rule `catalog.api.idempotency` states: every door that mints or retires an object, and every
    table write.

    Pinned as a ROSTER rather than one test each, because the failure this guards is a door ADDED later
    without the key — and a per-door test cannot notice a door nobody wrote a test for.

    A project's and a warehouse's create are not on it: a re-POST converges on the record, carrying the first one's
    identity forward.
    """

    def test_the_full_roster_declares_the_header(self, real_ns_client: TestClient) -> None:
        want = {
            "POST /v1/table/{id}/create",
            "POST /v1/table/{id}/declare",
            "POST /v1/table/{id}/drop",
            "POST /v1/table/{id}/rename",
            "POST /v1/table/{id}/deregister",
            "POST /v1/table/{id}/register",
            "POST /v1/table/{id}/restore",
            "POST /v1/table/{id}/insert",
            "POST /v1/table/{id}/merge_insert",
            "POST /v1/table/{id}/update",
            "POST /v1/table/{id}/delete",
            "POST /management/v1/table/{id}/undrop",
            "POST /v1/namespace/{id}/create",
            "POST /v1/namespace/{id}/drop",
            "POST /management/v1/namespace/{id}/undrop",
            "DELETE /management/v1/warehouses/{warehouse_id}",
            "DELETE /management/v1/projects/{project_id}",
        }
        keyed, _ = _keyed_doors(real_ns_client.app)

        assert want <= keyed, f"these doors a retry would apply again take no idempotency key: {sorted(want - keyed)}"
