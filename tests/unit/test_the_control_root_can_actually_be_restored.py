"""The control root's recoverability, exercised rather than assumed.

[[LH-110]]. The control root holds the estate's governance as small JSON objects — the project and
warehouse registries, the namespace bindings, protection, policies, gates, transforms, tasks, and the
trash records that make a drop recoverable. Lose them and the Lance data survives while every statement
about who may touch it does not.

NOTHING COULD HAVE TESTED IT BEFORE, which is why the row said "untested" rather than "broken".
Measured 2026-09-14: `backups.pgDump` covers the lineage and OpenFGA databases, `backups.volumeSnapshot`
covers the MinIO PVCs, both default OFF and neither is enabled in the live release — and this cluster
has no `volumesnapshotclass` resource type at all, so the snapshot path could not run here even if it
were switched on.

WHAT THIS PINS IS THE RESTORE, not the copy. A backup that runs is not a backup that restores, and the
difference is the whole point of the row: the tool writes a manifest of ETags and the restore compares
the bytes that landed against it, so "the loop finished" can never pass for "the records are back".

THE OUTBOX EXCLUSION IS THE ONE JUDGEMENT WORTH A TEST OF ITS OWN. `_control_outbox/` and
`_lineage_outbox/` are queues, not records: restoring them re-publishes events the estate has already
acted on — a replayed grant, a replayed drop announcement — which is worse than losing them. A queue's
correct recovery is to be empty, and a future reader "completing" the prefix list would undo that
silently.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("control_root_backup", REPO_ROOT / "scripts" / "control_root_backup.py")
assert _SPEC and _SPEC.loader
crb = importlib.util.module_from_spec(_SPEC)
sys.modules["control_root_backup"] = crb
_SPEC.loader.exec_module(crb)

BUCKET = "lance-catalog"
ROOT = f"s3://{BUCKET}"

#: One record per governed prefix, plus an outbox entry that must NOT travel — and the ZERO-BYTE
#: DIRECTORY MARKERS a real S3 store keeps. MinIO writes `_projects/` as an object; moto does not, and
#: that difference is not cosmetic: it broke the first version of this tool on the live estate while
#: every test here passed. The path join stripped the trailing slash, so `_projects/` became the key
#: `_projects` and all 92 records under it overwrote that one object — 1,464 copies landed as 9.
#: The double now carries the shape the real store has.
SEED = {
    "_projects/": b"",
    "_warehouses/": b"",
    "_trash/": b"",
    "_policies/state/": b"",
    "_projects/acme.json": b'{"id": "acme"}',
    "_warehouses/acme-wh.json": b'{"id": "acme-wh", "root_uri": "s3://acme-bucket"}',
    "_warehouses/bindings/acme-bronze.json": b'{"warehouse_id": "acme-wh"}',
    "_trash/table/acme-bronze$events.json": b'{"expires_at": "2026-10-01"}',
    "_protection/table/acme-bronze$events.json": b'{"protected": true}',
    "_policies/state/acme.json": b'{"retention_days": 30}',
    "_gates/acme.json": b'{"require_human": false}',
    "_transforms/acme/lane.json": b'{"task": "stage-transform"}',
    "_tasks/stage-transform.json": b'{"engine": "ray"}',
    "_control_outbox/pending-0001.json": b'{"action": "grant_added"}',
    "_lineage_outbox/pending-0002.json": b'{"eventType": "COMPLETE"}',
}


@pytest.fixture
def s3() -> Any:  # noqa: ANN401 — boto3 client has no public stub
    from moto import mock_aws

    with mock_aws():
        import boto3

        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        for key, body in SEED.items():
            client.put_object(Bucket=BUCKET, Key=key, Body=body)
        yield client


def test_a_backup_captures_every_RECORD_prefix(s3: Any) -> None:
    result = crb.do_backup(s3, root=ROOT, dest=None, stamp="20260914T000000Z")

    manifest = crb.read_manifest(s3, result["backup"])
    captured = set(manifest["objects"])
    records = {k for k in SEED if not k.startswith(("_control_outbox/", "_lineage_outbox/")) and not k.endswith("/")}

    assert captured == records, f"the backup does not match the record set; missing={records - captured} extra={captured - records}"
    assert result["objects"] == len(records)


def test_the_OUTBOXES_are_not_in_the_backup(s3: Any) -> None:
    """Restoring a queue re-publishes what the estate already handled. The exclusion is deliberate, so
    it gets a test that fails if somebody 'completes' the prefix list."""
    result = crb.do_backup(s3, root=ROOT, dest=None, stamp="20260914T000000Z")
    captured = set(crb.read_manifest(s3, result["backup"])["objects"])

    assert not [k for k in captured if k.startswith(("_control_outbox/", "_lineage_outbox/"))], captured
    assert crb.SKIPPED_PREFIXES, "the skipped set is empty, so nothing records that this was a decision"


def test_a_RESTORE_puts_the_bytes_back_and_proves_it(s3: Any) -> None:
    """THE ROW'S ACTUAL ASK. A backup that runs is not a backup that restores."""
    backup = crb.do_backup(s3, root=ROOT, dest=None, stamp="20260914T000000Z")["backup"]

    restored = crb.do_restore(s3, backup=backup, into=f"s3://{BUCKET}/_restore_probe", force=False)

    assert restored["verified"], restored
    assert not restored["mismatched"], restored
    for key, body in SEED.items():
        if key.startswith(("_control_outbox/", "_lineage_outbox/")) or key.endswith("/"):
            continue
        got = s3.get_object(Bucket=BUCKET, Key=f"_restore_probe/{key}")["Body"].read()
        assert got == body, f"{key} came back with different bytes"


def test_a_restore_over_the_LIVE_root_is_refused_without_force(s3: Any) -> None:
    """A restore is the one operation that can destroy what it exists to protect: run it at the wrong
    target, or against a root that has moved on, and current governance is replaced by old governance.
    The rehearsal form is the default and the destructive one is opt-in — which is also what makes this
    exercisable against a real estate."""
    backup = crb.do_backup(s3, root=ROOT, dest=None, stamp="20260914T000000Z")["backup"]

    with pytest.raises(SystemExit) as excinfo:
        crb.do_restore(s3, backup=backup, into=ROOT, force=False)

    assert "--force" in str(excinfo.value), excinfo.value


def test_verify_detects_a_backup_that_has_ROTTED(s3: Any) -> None:
    """`verify` answers "is this backup intact" BEFORE an incident. A check that passed on a corrupted
    backup would be worse than no check, so the corruption is introduced and the check must catch it."""
    backup = crb.do_backup(s3, root=ROOT, dest=None, stamp="20260914T000000Z")["backup"]
    assert crb.do_verify(s3, backup=backup)["intact"]

    _, prefix = crb.split_uri(backup)
    s3.put_object(Bucket=BUCKET, Key=f"{prefix}/_projects/acme.json", Body=b'{"id": "TAMPERED"}')

    report = crb.do_verify(s3, backup=backup)
    assert not report["intact"], report
    assert "_projects/acme.json" in report["changed"], report


def test_a_record_written_DURING_the_backup_is_captured_as_copied_and_REPORTED(s3: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live control root is written while it is being read, and the manifest must describe the bytes
    the backup HAS — not the version the listing happened to see.

    Measured on the live estate 2026-09-14: three `_tasks/` records changed between the list and the
    copy on a single run, so a manifest built from listed ETags declared a perfectly good backup
    corrupt. Recording the COPY's ETag makes `verify` mean "is this backup intact"; the race is
    reported separately, because "which records moved under the backup" is a real operator question
    and silently swallowing it would be the other kind of lie.
    """
    real = crb.list_objects

    def stale(client: Any, bucket: str, prefix: str) -> dict[str, str]:  # noqa: ANN401
        listed = real(client, bucket, prefix)
        # Exactly the live shape: the listing reports a version that is no longer current by copy time.
        return {k: ("stale-etag" if k == "_projects/acme.json" else v) for k, v in listed.items()}

    monkeypatch.setattr(crb, "list_objects", stale)
    result = crb.do_backup(s3, root=ROOT, dest=None, stamp="20260914T000000Z")
    monkeypatch.setattr(crb, "list_objects", real)

    assert result["raced"] == ["_projects/acme.json"], result
    assert crb.do_verify(s3, backup=result["backup"])["intact"], "a raced record must not make the backup read as corrupt"
    restored = crb.do_restore(s3, backup=result["backup"], into=f"s3://{BUCKET}/_probe2", force=False)
    assert restored["verified"], restored


def test_the_backup_states_which_failure_it_protects_against(s3: Any) -> None:
    """A same-bucket backup survives a bad write and not a lost bucket, and whoever finds it during an
    incident must not have to read the tool to learn which. It is written into the manifest."""
    same = crb.do_backup(s3, root=ROOT, dest=None, stamp="20260914T000000Z")
    assert "logical loss" in same["protects_against"]
    assert "loss of the source bucket" not in same["protects_against"]

    s3.create_bucket(Bucket="offsite")
    other = crb.do_backup(s3, root=ROOT, dest="s3://offsite/control", stamp="20260914T000001Z")
    assert "loss of the source bucket" in other["protects_against"]
    assert json.loads(s3.get_object(Bucket="offsite", Key="control/20260914T000001Z/MANIFEST.json")["Body"].read())["objects"]
