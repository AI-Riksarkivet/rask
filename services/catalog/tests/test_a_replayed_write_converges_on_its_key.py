"""A retried write must converge on the caller's key instead of re-executing.

THE DEFECT THIS CLOSES (§ Q17-32). A retrying client replays a write door's failure, and the
catalog's 91 write routes carried no idempotency key on any of them — so a 500 raised
AFTER the Lance write in `create_governed_table` (the schema read-back, `emit_create`, `emit_control`)
is replayed with nothing to converge it. With `mode=Create` the replays answer AlreadyExists, so a
fully successful create surfaces as a 409 nobody can tell from a name collision; with `mode=Overwrite`
each replay RE-EXECUTES the destructive path — drop and rewrite the dataset, strip and re-seed the
ACL, four `table_created` events for one logical create.

`python-infrastructure`'s rule offers exactly two outs — *"Always pair retry with an idempotency key
OR mark the operation non-retryable"* — and nothing the estate runs replays a catalog call. This is the
first out, for every client whose own retry the estate cannot see.

THE KEY IS OPTIONAL, AND THAT IS A SPEC CONSTRAINT RATHER THAN A COMPROMISE. The Lance Namespace spec
defines no `Idempotency-Key`, and `rask-lance-catalog` records that a stock Lance client must work with
no rask SDK — so requiring it would break conformance on 54 routed operations. A caller that sends one
gets convergence; one that does not is exactly where it was.

Driven on a real local control root. A rival lands between an attempt's read of the key and its write by running
inside that read, so the interleaving happens on every run; a claim stamped a day ago is one whose read of the store's
clock answered a day ago, the one double here, since a lease is timed on that clock alone ([[LH-576]]).
"""

from __future__ import annotations

import os
import tracemalloc
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from lance_namespace import LanceNamespace

from catalog.services.idempotency_records import PREFIX, InFlightError, KeyBinding, KeyReusedError, Replay, TakenOver, claim, reclaim, record_outcome, release
from lakehouse.storage import conditional_records
from lakehouse.storage.options import StorageOptions


SO = StorageOptions()
SCOPE = "acme"
KEY = "0b6f3f0e-5c7a-4a43-9d6e-2f1c8a9b7e10"
CREATE = KeyBinding(endpoint="POST /v1/table/{id}/create", target="db$t")
DIGEST = "0" * 64
LIFETIME = timedelta(days=1)


def _claimed_a_day_ago(root: str, monkeypatch: pytest.MonkeyPatch, *, attempt: str) -> None:
    """``attempt`` claimed the key a day ago on the store's clock, and never finished."""
    read = conditional_records.read_json_at
    monkeypatch.setattr(conditional_records, "read_json_at", lambda *args: read(*args).model_copy(update={"at": datetime.now(UTC) - LIFETIME}))
    claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt=attempt)
    monkeypatch.setattr(conditional_records, "read_json_at", read)


class TestTheFirstAttemptOwnsTheKey:
    def test_two_scopes_do_not_collide_on_one_key(self, tmp_path: Path) -> None:
        """A key is the CALLER's, so the same string from two tenants is two different operations."""
        assert claim(str(tmp_path), SO, scope="acme", key=KEY, binding=CREATE, digest=DIGEST, attempt="a") is None
        assert claim(str(tmp_path), SO, scope="other", key=KEY, binding=CREATE, digest=DIGEST, attempt="b") is None


class TestAnAttemptStillRunningIsNotAReplay:
    def test_two_takeovers_of_an_expired_claim_run_the_write_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two retries arriving after the lease both find the claim abandoned; the store lets one take the key over,
        and the other, whose write follows a read the first one's takeover outdated, finds the key held."""
        root = str(tmp_path)
        _claimed_a_day_ago(root, monkeypatch, attempt="crashed")
        read = conditional_records.read_json_at
        rival: list[object] = []

        def _read_then_take_over(root_uri: str, storage_options: StorageOptions, key: str) -> conditional_records.StoreRead:
            found = read(root_uri, storage_options, key)
            if not rival:
                rival.append("taking over")
                rival[0] = claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="rival")
            return found

        monkeypatch.setattr(conditional_records, "read_json_at", _read_then_take_over)

        with pytest.raises(InFlightError):
            claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="late")
        assert [type(taken) for taken in rival] == [TakenOver], "the rival's takeover must run, alone, told it took over a lapsed attempt"

    def test_reclaiming_does_not_lose_the_outcome_the_first_attempt_then_writes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The abandoned attempt may not be dead, only slow. Whichever finishes writes the outcome, and
        a later replay must see one — never a record still marked in-flight."""
        root = str(tmp_path)
        _claimed_a_day_ago(root, monkeypatch, attempt="slow")
        claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="retry")
        record_outcome(root, SO, scope=SCOPE, key=KEY, status=200, body={"ok": True})

        assert claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="third") == Replay(status=200, body={"ok": True})

    def test_a_release_by_an_attempt_whose_claim_was_taken_over_frees_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """[[LH-487]] An attempt releases only the claim it made: once another attempt has taken the key over, the
        first one's release leaves the key held, so a third attempt still waits."""
        root = str(tmp_path)
        _claimed_a_day_ago(root, monkeypatch, attempt="first")
        claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="second")

        release(root, SO, scope=SCOPE, key=KEY, attempt="first")

        with pytest.raises(InFlightError):
            claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="third")

    def test_a_claim_given_back_is_taken_by_the_corrected_request_and_bound_to_it(self, tmp_path: Path) -> None:
        """[[LH-487]], XC-219. A claim given back was refused before it wrote, so the caller's corrected request, another
        request, takes the key under its own digest; the key then refuses the request it was given back by."""
        root = str(tmp_path)
        assert claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="refused") is None
        release(root, SO, scope=SCOPE, key=KEY, attempt="refused")

        assert claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest="1" * 64, attempt="corrected") is None
        record_outcome(root, SO, scope=SCOPE, key=KEY, status=200, body={"ok": True})
        with pytest.raises(KeyReusedError, match="different request"):
            claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="third")


class TestARecordOutlivesItsLifetimeByNoReclaim:
    def test_a_takeover_landing_inside_the_reclaim_keeps_its_record(self, tmp_path: Path) -> None:
        """A record no attempt wrote for its lifetime is reclaimed, but a retry that takes its key over between the
        reclaim's read and its delete writes the record again: the reclaim leaves it, so the attempt it admitted still
        holds the key and a third retry waits rather than running the write again."""
        root = str(tmp_path)
        claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="released")
        release(root, SO, scope=SCOPE, key=KEY, attempt="released")
        record = Path(root, PREFIX, SCOPE, f"{KEY}.json")
        a_day_ago = (datetime.now(UTC) - LIFETIME - timedelta(minutes=1)).timestamp()
        os.utime(record, (a_day_ago, a_day_ago))
        read = conditional_records.read_json_at
        taken: list[object] = []

        def _read_then_take_over(root_uri: str, storage_options: StorageOptions, key: str) -> conditional_records.StoreRead:
            found = read(root_uri, storage_options, key)
            if not taken:
                taken.append("taking over")
                taken[0] = claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="retry")
            return found

        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(conditional_records, "read_json_at", _read_then_take_over)
            reclaimed = reclaim(root, SO, lifetime=LIFETIME, limit=10)

        assert (reclaimed.reclaimed, reclaimed.kept, taken) == (0, 1, [None])
        with pytest.raises(InFlightError):
            claim(root, SO, scope=SCOPE, key=KEY, binding=CREATE, digest=DIGEST, attempt="third")


class TestTheKeyIsNeverAPathTraversal:
    @pytest.mark.parametrize("bad", ["..", KEY.upper()])
    def test_a_key_that_is_not_a_canonical_uuid_is_refused(self, tmp_path: Path, bad: str) -> None:
        """The key becomes part of an object key, so an unvalidated one writes outside its own prefix. A canonical
        lower-case UUID is the one spelling that cannot. The doors constrain it at the header too; this refuses it
        at the seam, because a second caller of this module must not have to remember."""
        with pytest.raises(ValueError, match="idempotency key"):
            claim(str(tmp_path), SO, scope=SCOPE, key=bad, binding=CREATE, digest=DIGEST, attempt="a")


TABLE = "/v1/table/lance_catalog$db$t"
FIRST_KEY = "0b6f3f0e-5c7a-4a43-9d6e-2f1c8a9b7e11"
_RENAME = {"new_table_name": "moved"}
#: Whatever the store holds under a trash record's key, once it is no record.
_NOT_A_RECORD = b"not a trash record"


def _rows(*ids: int) -> bytes:
    from lakehouse.arrow_ipc import encode_arrow_stream

    return encode_arrow_stream(pa.table({"id": pa.array(list(ids), pa.int64())}))


def _keyed(catalog: Any, who: str, key: str | None, door: str, query: dict[str, str], body: dict[str, Any] | None, *, table: str = TABLE) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE

    headers = {**catalog.bearer(who), **({"Idempotency-Key": key} if key else {})}
    if door == "create":
        return catalog.client.post(f"{table}/create", params=query, content=_rows(1), headers={**headers, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    return catalog.client.post(f"{table}/{door}", params=query, json=body, headers=headers)


def _seen(answer: Any) -> tuple[int, dict[str, Any]]:  # noqa: ANN401 — an httpx response
    """What a caller is told, less the per-request ``instance`` a problem names."""
    return answer.status_code, {name: value for name, value in answer.json().items() if name != "instance"}


@pytest.mark.parametrize(
    ("case", "door", "query", "body"),
    [
        pytest.param("answered", "drop", {}, None, id="1-a-recoverable-drop-answered"),
        pytest.param("lost", "drop", {}, None, id="2-a-recoverable-drop-whose-answer-was-lost"),
        pytest.param("grants-gone", "deregister", {}, {}, id="3-a-deregister-which-takes-the-grants"),
        pytest.param("answered", "rename", {}, _RENAME, id="4-a-rename-answered"),
        pytest.param("no-record", "drop", {}, None, id="5-a-caller-holding-no-record"),
        pytest.param("another-caller", "drop", {}, None, id="6-another-caller-presenting-the-key"),
        pytest.param("revoked", "drop", {}, None, id="7-the-caller-after-their-grants-went"),
        pytest.param("grants-gone", "drop", {"purge": "true"}, None, id="8-a-destroying-drop"),
        pytest.param("retired-by-another", "drop", {}, None, id="9-another-request-dropped-it-first"),
        pytest.param("died-before-the-change", "create", {}, None, id="10-a-create-lost-before-it-changed-anything"),
        pytest.param("lost", "restore", {}, {"version": 1}, id="11-a-restore-whose-answer-was-lost"),
        pytest.param("evidence-unreadable", "drop", {}, None, id="12-a-drop-whose-trash-record-cannot-be-read"),
        pytest.param("died-before-the-detach", "drop", {}, None, id="13-a-drop-lost-before-its-detach-then-renamed"),
        pytest.param("refused-first", "drop", {}, None, id="14-a-drop-refused-then-dropped-by-another"),
        pytest.param("another-door", "drop", {}, None, id="15-the-key-on-another-door"),
        pytest.param("another-request", "drop", {}, None, id="16-the-key-with-another-request"),
        pytest.param("another-name", "drop", {}, None, id="17-the-key-on-another-name"),
        pytest.param("stamp-refused", "drop", {}, None, id="18-a-drop-whose-stamp-the-store-refuses"),
    ],
)
def test_a_keyed_resend_is_answered_as_its_door_declares_its_takeover(
    governed_catalog: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    door: str,
    query: dict[str, str],
    body: dict[str, Any] | None,
) -> None:
    """XC-219. A keyed drop or rename that landed leaves its name holding nothing, so its resend is authorized against the
    object the key was first claimed on: answered with the first answer, or, for a recoverable drop whose answer was lost
    past the lease, with the answer its trash record proves. Anyone else, or the caller once the rung no longer admits
    them, is refused exactly as on any missing name, and so is the key on another door, name or request, or one whose
    first request was refused; a destroying drop and a deregister took the grants, so their resend is refused too. A
    takeover the evidence does not prove runs again against the state it finds (a drop another request landed first
    refuses 404; a create lost before it changed anything creates; a drop lost before its detach, its table renamed since,
    is not answered as done), and is refused as of unknown outcome where a second run would apply again (a restore) or the
    evidence cannot be read."""
    from catalog.api import idempotency as keyed_doors
    from catalog.core.config import get_settings
    from catalog.services import dataplane, idempotency_records, native, table_claims
    from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE
    from lakehouse.records import trash
    from lakehouse.storage.objectfs import fs_and_base

    catalog = governed_catalog
    settings = get_settings().model_copy(update={"trash_grace_days": 1.0})
    catalog.client.app.dependency_overrides[get_settings] = lambda: settings
    assert catalog.create_in_the_estate("alice", "db").status_code == 200
    alice = catalog.bearer("alice")
    arrow = {**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE}
    assert catalog.client.post(f"{TABLE}/create", content=_rows(1), headers=arrow).status_code == 200
    assert catalog.client.post(f"{TABLE}/insert", params={"mode": "append"}, content=_rows(2), headers=arrow).status_code == 200
    uuid = catalog.client.post(f"{TABLE}/describe", json={}, headers=alice).json()["properties"]["rask.uuid"]
    table = "/v1/table/lance_catalog$db$fresh" if door == "create" else TABLE

    async def lost(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the pod ended before the answer was recorded")

    def faults(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the pod ended before the door changed anything")

    detach = native.call

    def detach_faults(ns: LanceNamespace, op: str, request: object) -> object:
        if op == "deregister_table":
            raise RuntimeError("the pod ended after the trash record was filed, before the detach")
        return detach(ns, op, request)

    def store_refuses(*_args: object, **_kwargs: object) -> bool:
        raise OSError("the store refused the write")

    if case == "refused-first":
        protected = catalog.client.post("/management/v1/table/lance_catalog$db$t/protection", json={"protected": True}, headers=alice)
        assert protected.status_code == 200, protected.text
    with monkeypatch.context() as during:
        if case in {"lost", "evidence-unreadable"}:
            during.setattr(keyed_doors.Converge, "remember", lost)
        if case == "retired-by-another":
            during.setattr(table_claims, "fence", faults)
        if case == "died-before-the-change":
            during.setattr(dataplane, "create_table", faults)
        if case == "died-before-the-detach":
            during.setattr(native, "call", detach_faults)
        if case == "stamp-refused":
            during.setattr(trash, "stamp_detached", store_refuses)
        try:
            first = _keyed(catalog, "alice", FIRST_KEY, door, query, body, table=table)
        except RuntimeError:
            first = None
    if case == "refused-first":
        assert first is not None and first.status_code == 409, first.text if first is not None else None
        assert catalog.client.post(f"{TABLE}/drop", params={"force": "true"}, headers=alice).status_code == 200
    if case == "retired-by-another":
        assert catalog.client.post(f"{TABLE}/drop", headers=alice).status_code == 200
    if case == "died-before-the-detach":
        assert catalog.client.post(f"{TABLE}/rename", json={"new_table_name": "m"}, headers=alice).status_code == 200
    if case == "evidence-unreadable":
        fs, base = fs_and_base(settings.registry_root, settings.storage_options())
        with fs.open_output_stream(f"{base}/{trash.key(uuid)}") as planted:
            planted.write(_NOT_A_RECORD)
    if first is None:
        monkeypatch.setattr(idempotency_records, "claim", partial(idempotency_records.claim, lease=timedelta(0)))
    if case == "revoked":
        catalog.store.delete(*[held for held in catalog.store.tuples() if held[0] == catalog.person("alice")])
    who, key = {"no-record": ("alice", "0b6f3f0e-5c7a-4a43-9d6e-2f1c8a9b7e12"), "another-caller": ("bob", FIRST_KEY)}.get(case, ("alice", FIRST_KEY))
    # The resend's door, query and name, where it is not the first request's.
    door, query, body, table = {
        "another-door": ("deregister", {}, None, table),
        "another-request": ("drop", {"purge": "true"}, None, table),
        "another-name": ("drop", {}, None, "/v1/table/lance_catalog$db$other"),
    }.get(case, (door, query, body, table))

    again = _keyed(catalog, who, key, door, query, body, table=table)

    if case == "died-before-the-detach":
        assert (again.status_code, again.json().get("code")) == (404, 4), f"a drop that never detached must not be answered as done: {again.text}"
        assert catalog.client.post("/v1/table/lance_catalog$db$m/describe", json={}, headers=alice).status_code == 200, "the table must still be readable"
    elif case == "stamp-refused":
        dropped = {"transaction_id": f"lance_catalog$db$t${uuid}"}
        assert first is not None and _seen(first) == (200, dropped), first.text if first is not None else None
        assert trash.get(settings.registry_root, settings.storage_options(), uuid) is not None, "the drop must have filed its trash record"
        assert _seen(again) == (200, dropped), again.text
    elif case == "answered" or (case == "lost" and door == "drop"):
        answered = {"transaction_id": f"lance_catalog$db$t${uuid}"} if first is None else first.json()
        assert _seen(again) == (200, answered), again.text
    elif case == "retired-by-another":
        assert (again.status_code, again.json().get("code")) == (404, 4), again.text
    elif case == "died-before-the-change":
        assert again.status_code == 200, again.text
        assert catalog.client.post(f"{table}/describe", json={}, headers=alice).status_code == 200, "the resend must have created the table"
    elif case in {"lost", "evidence-unreadable"}:
        assert (again.status_code, again.json().get("code")) == (409, 14), again.text
    else:
        assert _seen(again) == _seen(_keyed(catalog, who, None, door, query, body, table=table)), "the refusal must be the one any missing name gets"
        assert again.status_code == 403, again.text


class TestARequestIsDigestedWithoutACopy:
    def test_digesting_a_body_allocates_no_block_of_its_size(self) -> None:
        """XC-219. A keyed request's body, up to the body cap, is digested where it lies: a second copy of it would double
        the memory a maximum-size write holds."""
        from catalog.api.idempotency import request_digest

        body = os.urandom(8 << 20)
        tracemalloc.start()
        try:
            request_digest(body, query=[("mode", "append")], headers={})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert peak < 1 << 20, f"digesting an 8 MiB body peaked at {peak} bytes"
