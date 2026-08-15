"""F4 (`open_lakehouse_diff2.md`) — lost-update detection on control-root read-modify-writes.

F1 closed the CREATE race with put-if-not-exists. It could not close the MUTATION race, because a
mutation's key exists by definition — that is what makes it a mutation. So every mutable registry
write stayed `get → mutate a dict → put` with no precondition, and a quarantine could be lifted
without anyone calling `/activate`:

    t0  a GitOps re-POST of warehouse `acme-wh` reads the record        (status=active)
    t1  an operator POSTs /deactivate                                    (status=deactivated)
    t2  the re-POST's put lands, writing the record it built at t0       (status=active)

Nothing in that sequence is a wrong decision by either writer. The re-POST is not stale in a field
it meant to change — it is stale in `status`, a field it merely carried forward. That distinction is
the whole finding: a guard on the DECISION cannot see it, and only a guard on the WRITE can.

The interleave below is DETERMINISTIC, not threaded: the rival write is performed inside the
`mutate` callback, i.e. exactly between this writer's read and its write. A thread-and-hope test
would pass on a machine that happens to schedule kindly, which for a race is the same as no test.

Local-FS is the branch exercised directly (`flock` + content-hash ETag — cooperating writers on one
host, which is the whole scope of a local root); the S3 branch is driven through a fake boto3 client
pinning the `IfMatch` parameter and the 412 → `RecordChangedError` mapping, mirroring
`test_registry_cas.py`'s treatment of the create branch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from catalog.services import warehouses

from service_kit.lakehouse import records


def _root(tmp_path: Any) -> str:
    return f"file://{tmp_path}"


def _client_error(code: str, status: int) -> ClientError:
    """A ClientError shaped like botocore's own — EVERY `ResponseMetadata` key, not just the one read.

    `_ResponseMetadataTypeDef` is a total TypedDict and botocore always populates all five; a partial
    one is a fake no real call can produce, and the `.get()` chains in `records` would swallow the
    difference until a consumer grew a `RetryAttempts` read and KeyError'd in production.

    Duplicated from `test_registry_cas.py` rather than shared: pytest here runs with
    `--import-mode=importlib`, so test modules are not importable from one another, and the
    alternative — hoisting fifteen lines into `tests/unit/conftest.py` — would put a boto3-shaped
    fixture in front of every unit test in the directory to save one copy.
    """
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {
                "RequestId": "req-test",
                "HostId": "host-test",
                "HTTPStatusCode": status,
                "HTTPHeaders": {},
                "RetryAttempts": 0,
            },
        },
        "PutObject",
    )


def _seed(tmp_path: Path, warehouse_id: str = "acme-wh", **fields: str) -> dict[str, str]:
    record = {"id": warehouse_id, "bucket": "b-acme", "project": "acme", "status": "active", **fields}
    records.create_json(_root(tmp_path), {}, f"_warehouses/{warehouse_id}.json", record)
    return record


# --------------------------------------------------------------------------- #
# the seam: mutate_json
# --------------------------------------------------------------------------- #


def test_mutate_applies_and_returns_the_written_record(tmp_path: Path) -> None:
    _seed(tmp_path)
    out = records.mutate_json(_root(tmp_path), {}, "_warehouses/acme-wh.json", lambda r: {**r, "status": "deactivated"})
    assert out["status"] == "deactivated"
    on_disk, _etag = records.read_json(_root(tmp_path), {}, "_warehouses/acme-wh.json") or ({}, "")
    assert on_disk["status"] == "deactivated"
    assert on_disk["project"] == "acme"  # untouched fields survive


def test_mutate_reapplies_against_the_winner_when_the_record_moves_mid_flight(tmp_path: Path) -> None:
    """THE F4 TEST. A rival write lands between this writer's read and its write.

    The rival's field must survive (it was never ours to carry forward) AND our own change must still
    land (we were asked to make it). A last-writer-wins put would silently revert the rival.
    """
    _seed(tmp_path)
    key = "_warehouses/acme-wh.json"
    seen: list[str] = []

    def mutate(record: dict[str, Any]) -> dict[str, Any]:
        seen.append(str(record.get("note", "")))
        if len(seen) == 1:
            # The interleave: a rival writes a DIFFERENT field, after our read, before our write.
            records.mutate_json(_root(tmp_path), {}, key, lambda r: {**r, "note": "quarantined by ops"})
        return {**record, "status": "deactivated"}

    out = records.mutate_json(_root(tmp_path), {}, key, mutate)

    assert len(seen) == 2, "the first attempt must LOSE and be retried against the winner's record"
    assert seen == ["", "quarantined by ops"], "the retry must re-read, not replay its own stale copy"
    assert out["status"] == "deactivated"  # our change landed…
    assert out["note"] == "quarantined by ops"  # …and the rival's field was NOT reverted


def test_mutate_raises_when_the_record_is_absent(tmp_path: Path) -> None:
    """Distinct from a lost race on purpose: "somebody else wrote" and "there is nothing here" call
    for different answers at the door (409 vs 404), so the seam must not collapse them."""
    with pytest.raises(records.RecordMissingError):
        records.mutate_json(_root(tmp_path), {}, "_warehouses/ghost.json", lambda r: r)


def test_mutate_gives_up_after_bounded_attempts(tmp_path: Path) -> None:
    """A pathological livelock becomes an honest error rather than an unbounded spin."""
    _seed(tmp_path)
    key = "_warehouses/acme-wh.json"
    rounds = {"n": 0}

    def always_lose(record: dict[str, Any]) -> dict[str, Any]:
        rounds["n"] += 1
        # Rewrite the record on EVERY attempt, so this writer can never win.
        records.create_json  # noqa: B018 — referenced for readers; the write below is the rival
        path = Path(str(tmp_path)) / key
        path.write_text(json.dumps({**record, "spin": rounds["n"]}))
        return {**record, "status": "deactivated"}

    with pytest.raises(records.RecordChangedError):
        records.mutate_json(_root(tmp_path), {}, key, always_lose, attempts=3)
    assert rounds["n"] == 3


def test_read_json_returns_none_for_a_missing_key(tmp_path: Path) -> None:
    assert records.read_json(_root(tmp_path), {}, "_warehouses/nope.json") is None


# --------------------------------------------------------------------------- #
# the S3 branch — the precondition is the POINT, so it is pinned by name
# --------------------------------------------------------------------------- #


def test_s3_replace_sends_if_match_and_maps_412_to_a_lost_race(monkeypatch: pytest.MonkeyPatch) -> None:
    """`IfMatch` is what makes this safe on the multi-replica path; a refactor that drops the kwarg
    would leave every call site reading as conditional while writing unconditionally — the F4 bug
    restored, invisibly. So the parameter is asserted by name, not just the behaviour."""
    client = MagicMock()
    client.get_object.return_value = {"Body": MagicMock(read=lambda: b'{"id":"w","status":"active"}'), "ETag": '"abc123"'}
    client.put_object.side_effect = _client_error("PreconditionFailed", 412)
    monkeypatch.setattr(records, "_s3_client", lambda _so: client)

    with pytest.raises(records.RecordChangedError):
        records.mutate_json("s3://ctl/root", {}, "_warehouses/w.json", lambda r: {**r, "status": "deactivated"}, attempts=1)

    assert client.put_object.call_args.kwargs["IfMatch"] == "abc123"
    assert client.put_object.call_args.kwargs["Key"] == "root/_warehouses/w.json"


def test_s3_read_surfaces_a_non_404_error_instead_of_reporting_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An outage must not be laundered into "no such record" — that would turn a 503 into a silent
    404 and, one level up, into a create that overwrites a record it could not read."""
    client = MagicMock()
    client.get_object.side_effect = _client_error("AccessDenied", 403)
    monkeypatch.setattr(records, "_s3_client", lambda _so: client)
    with pytest.raises(Exception, match="AccessDenied"):
        records.read_json("s3://ctl/root", {}, "_warehouses/w.json")


# --------------------------------------------------------------------------- #
# the door: set_warehouse_status
# --------------------------------------------------------------------------- #


def test_set_warehouse_status_flips_and_preserves_other_fields(tmp_path: Path) -> None:
    _seed(tmp_path)
    out = warehouses.set_warehouse_status(_root(tmp_path), {}, "acme-wh", "deactivated")
    assert out is not None
    assert out["status"] == "deactivated"
    assert out["bucket"] == "b-acme"
    assert warehouses.warehouse_status(_root(tmp_path), {}, "acme-wh") == "deactivated"


def test_set_warehouse_status_returns_none_for_an_unknown_warehouse(tmp_path: Path) -> None:
    """RecordMissingError is translated at the door, so the endpoint's existing 404 path is unchanged
    — the seam swap must not become an API change."""
    assert warehouses.set_warehouse_status(_root(tmp_path), {}, "ghost", "deactivated") is None


def test_a_quarantine_survives_a_concurrent_stale_carry_forward(tmp_path: Path) -> None:
    """The finding's own failure scenario, end to end at the service layer.

    A deactivate lands between a re-POST's read and its write. The re-POST carries `status` forward
    from its stale read — the documented behaviour, and correct in the sequential case. Under the
    conditional write its stale put is refused, it re-reads, and the quarantine SURVIVES.
    """
    root = _root(tmp_path)
    _seed(tmp_path)

    # t0 — the GitOps re-POST reads the record it will later carry forward.
    stale = warehouses.get_warehouse(root, {}, "acme-wh")
    assert stale is not None and stale["status"] == "active"

    # t1 — the operator quarantines it.
    warehouses.set_warehouse_status(root, {}, "acme-wh", "deactivated")

    # t2 — the re-POST writes the record it built at t0. Through the conditional seam this cannot
    # revert `status`: it re-reads and re-applies only the fields it actually owns.
    records.mutate_json(root, {}, "_warehouses/acme-wh.json", lambda r: {**r, "bucket": stale["bucket"]})

    assert warehouses.warehouse_status(root, {}, "acme-wh") == "deactivated", (
        "the quarantine was lifted by interleaving — the exact outcome the carry-forward comment claims to prevent, and could only prevent sequentially"
    )
