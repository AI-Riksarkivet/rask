"""CONTRACT: dropping a staged outbox event issues a DELETE and nothing else.

THE DRAIN HAD NEVER WORKED, and the reason is that a delete was not a delete. `drop_event` went
through pyarrow's `S3FileSystem.delete_file`, which re-creates the parent directory marker after
removing the object — so the DELETE path issues a **PutObject** on the prefix itself. Measured on the
deployed estate 2026-09-10 by staging two probe events and reading the strand:

    lineage_outbox_event_stranded
      error="When creating key '_lineage_outbox/' in bucket 'lance-catalog':
             AWS Error ACCESS_DENIED during PutObject operation: Access Denied"

The chart's policy is not the bug. Its statement is named `DrainItsOwnOutboxAndNothingElse` and grants
exactly `s3:DeleteObject` on `*/_lineage_outbox/*`, and
`test_the_lineage_plane_writes_nothing_it_does_not_own.py` pins that lineage gets NO `PutObject`
anywhere — measured against the source, because the only bytes lineage changes are these deletes.
Widening the policy would grant a capability the service has no code to use AND leave a delete path
that writes; the mechanism is what was wrong.

WHY IT MATTERED MORE THAN A STRANDED OBJECT. `reconcile_cron` ingests, re-publishes, THEN drops. The
first two succeeded, so every staged event was re-delivered to `lineage.events.v1` on every tick —
forever, since the drop is the only thing that ends the loop. Subscribers include the medallion's
`/bronze-arrival`, which starts the cascade. The relay's own comment argues a duplicate is safe under
at-least-once, which is true of ONE redelivery and false of an unbounded one.
"""

from __future__ import annotations

from typing import Any

import pytest

from service_kit.lakehouse import outbox


class _RecordingS3:
    """A stand-in for the storage seam's client that records the calls it is asked to make."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def delete_object(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("delete_object", kw))
        return {}

    def put_object(self, **kw: Any) -> dict[str, Any]:  # pragma: no cover — the assertion is that this is never reached
        self.calls.append(("put_object", kw))
        return {}


def test_dropping_an_s3_event_issues_ONLY_a_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole contract: one DeleteObject, addressed at the event, and no write of any kind."""
    client = _RecordingS3()
    monkeypatch.setattr(outbox, "s3_client", lambda *_a, **_kw: client)

    outbox.drop_event("s3://lance-catalog/_lineage_outbox", {"endpoint": "http://rustfs:9000"}, "run-1@COMPLETE")

    assert [name for name, _ in client.calls] == ["delete_object"], f"the delete path made {client.calls}"
    _, kw = client.calls[0]
    assert kw["Bucket"] == "lance-catalog"
    assert kw["Key"] == "_lineage_outbox/run-1@COMPLETE.json"


def test_an_absent_object_is_idempotent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A staged object another replica already drained must not strand the tick.

    `delete_object` is idempotent on S3 by definition, but a backend that raises `NoSuchKey` must be
    tolerated for the same reason the pyarrow path suppressed `FileNotFoundError`: the post-condition
    the caller wants — the object is gone — already holds.
    """

    class _Missing(_RecordingS3):
        def delete_object(self, **kw: Any) -> dict[str, Any]:
            self.calls.append(("delete_object", kw))
            raise FileNotFoundError("NoSuchKey")

    monkeypatch.setattr(outbox, "s3_client", lambda *_a, **_kw: _Missing())
    outbox.drop_event("s3://lance-catalog/_lineage_outbox", {"endpoint": "http://rustfs:9000"}, "gone@COMPLETE")


def test_a_LOCAL_outbox_still_drops_through_the_filesystem(tmp_path) -> None:
    """The non-S3 path is unchanged — tests and dev stacks stage to a local directory."""
    uri = str(tmp_path / "outbox")
    outbox.stage_event(uri, {}, "run-2", '{"eventType": "COMPLETE"}')
    staged = list((tmp_path / "outbox").iterdir())
    assert staged, "nothing was staged, so the drop below would prove nothing"
    outbox.drop_event(uri, {}, "run-2@COMPLETE")
    assert not [p for p in (tmp_path / "outbox").iterdir() if p.suffix == ".json"]


@pytest.mark.parametrize(
    ("options", "spelling"),
    [
        ({"endpoint": "http://rustfs:9000", "aws_access_key_id": "K", "aws_secret_access_key": "S"}, "aws_-prefixed"),
        ({"endpoint": "http://rustfs:9000", "access_key_id": "K", "secret_access_key": "S"}, "bare"),
    ],
)
def test_the_delete_reads_BOTH_credential_spellings(monkeypatch: pytest.MonkeyPatch, options: dict[str, str], spelling: str) -> None:
    """The estate uses two spellings for one credential, and a reader that knows one signs as nobody.

    `lance_storage_options` emits `aws_`-prefixed keys — deliberately, because the bare ones do not
    displace a pod's ambient `AWS_*` environment and object_store blends the two sources. The catalog's
    own `storage_options()` returns the BARE form (measured 2026-09-10 against the running pod). So the
    outbox seam is handed both, depending on which service is draining.

    Reading one leaves the other `None` and boto3 falls back to its default chain. The lineage pod
    carries no ambient `AWS_*` (measured 2026-09-10), so the result is a delete with NO credential —
    the object stays staged and the drain is exactly as broken as before, which is a deployed fix that
    changes nothing rather than one that fails loudly. No test process has an ambient `AWS_*` either,
    so that spelling passes every unit test; this asserts the credential ARRIVED, not that a call
    was made.
    """
    seen: dict[str, object] = {}

    def _factory(*_a: object, **kw: object) -> _RecordingS3:
        seen.update(kw)
        return _RecordingS3()

    monkeypatch.setattr(outbox, "s3_client", _factory)
    outbox.drop_event("s3://lance-catalog/_lineage_outbox", options, "run-3@COMPLETE")

    assert seen.get("access_key") == "K", f"the {spelling} access key never reached the client: {seen}"
    assert seen.get("secret_key") == "S", f"the {spelling} secret key never reached the client: {seen}"


def test_staging_an_s3_event_issues_ONLY_a_put(monkeypatch: pytest.MonkeyPatch) -> None:
    """The twin of the delete contract: one PutObject, addressed at the event, and no bucket probe.

    `stage_event` went through pyarrow — `fs.create_dir(base, recursive=True)` under a comment calling
    an S3 prefix marker "harmless". It is not harmless to a credential that was scoped to the prefix:
    `create_dir` first tests that the BUCKET exists, and a session policy naming
    `arn:aws:s3:::lance-catalog/_lineage_outbox/*` grants nothing on the bucket itself.

    MEASURED on the deployed estate 2026-09-10, from the ingest pod, with a freshly vended STS
    credential in hand:

        OSError: When testing for existence of bucket 'lance-catalog':
                 AWS Error ACCESS_DENIED during HeadBucket operation

    So the credential half of CP-007 was correct and observed working — the door vended
    `aws_session_token` and an expiry — and the write still could not happen. Widening the session
    policy to cover the bucket is the wrong direction: it would grant a capability the stager has no
    need for, to keep a mechanism that writes an object nobody reads.
    """
    client = _RecordingS3()
    monkeypatch.setattr(outbox, "s3_client", lambda *_a, **_kw: client)

    outbox.stage_event("s3://lance-catalog/_lineage_outbox", {"endpoint": "http://rustfs:9000"}, "run-9", '{"eventType": "COMPLETE"}')

    assert [name for name, _ in client.calls] == ["put_object"], f"the stage path made {client.calls}"
    _, kw = client.calls[0]
    assert kw["Bucket"] == "lance-catalog"
    assert kw["Key"] == "_lineage_outbox/run-9@COMPLETE.json"
    assert kw["Body"] == b'{"eventType": "COMPLETE"}'


@pytest.mark.parametrize(
    ("options", "spelling"),
    [
        ({"endpoint": "http://rustfs:9000", "aws_access_key_id": "K", "aws_secret_access_key": "S", "aws_session_token": "T"}, "aws_-prefixed"),
        ({"endpoint": "http://rustfs:9000", "access_key_id": "K", "secret_access_key": "S", "session_token": "T"}, "bare"),
    ],
)
def test_the_stage_reads_BOTH_credential_spellings(monkeypatch: pytest.MonkeyPatch, options: dict[str, str], spelling: str) -> None:
    """The same two spellings the delete faces, and the stage faces them from MORE directions.

    The catalog's vending door returns the `aws_`-prefixed form while a service staging with its own
    configured store passes the bare one, so both genuinely arrive here. The SESSION TOKEN is asserted
    too and is the half with the sharper failure: a vended STS credential without it is not a weaker
    credential, it is an invalid one, and dropping it fails open onto whatever ambient chain boto3
    finds.
    """
    seen: dict[str, object] = {}

    def _factory(*_a: object, **kw: object) -> _RecordingS3:
        seen.update(kw)
        return _RecordingS3()

    monkeypatch.setattr(outbox, "s3_client", _factory)
    outbox.stage_event("s3://lance-catalog/_lineage_outbox", options, "run-8", '{"eventType": "FAIL"}')

    assert seen.get("access_key") == "K", f"the {spelling} access key never reached the client: {seen}"
    assert seen.get("secret_key") == "S", f"the {spelling} secret key never reached the client: {seen}"
    assert seen.get("session_token") == "T", f"the {spelling} session token never reached the client: {seen}"
