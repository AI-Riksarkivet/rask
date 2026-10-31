"""#50 maintenance policies — the shared registry, the sweep's policy logic, and the protection proof.

The registry round-trips against a local filesystem root (``fs_and_base`` falls back to the local FS).
The tag-protection test is real Lance, not a mock: it pins the load-bearing claim that a tag-pinned
version survives any retention policy (``cleanup_old_versions`` exempts tagged versions), because the
whole feature is unsafe if that ever regresses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from maintenance.core.config import MaintenanceSettings
from maintenance.services.expiry import ExpiryPolicy, ExpiryTarget, expire_versions
from maintenance.services.plan import cadence_skip
from service_kit.lakehouse import maintenance_policies as mp
from service_kit.lakehouse.objectfs import StorageOptions


TABLE_UUID = "0b1e5c1a-0000-4000-8000-000000000001"


def _policy(kind: str = "table", id_: str = "db$t", uuid: str = TABLE_UUID, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "id": id_, "uuid": uuid, "compact_enabled": True, **fields}


def _project_policy(id_: str = "acme", buckets: list[str] | None = None, **fields: Any) -> dict[str, Any]:
    # #84 shape: no uuid — a project record matches by its warehouse BUCKETS (resolved at set time).
    return {
        "kind": "project",
        "id": id_,
        "buckets": buckets if buckets is not None else ["acme-wh"],
        "compact_enabled": True,
        **fields,
    }


def test_registry_round_trips_and_lists(tmp_path: Path) -> None:
    root = str(tmp_path)
    mp.put_policy(root, StorageOptions(), _policy(retention_days=7))
    mp.put_policy(root, StorageOptions(), _policy(kind="namespace", id_="silver", uuid="ns-silver"))

    stored = mp.get_policy(root, StorageOptions(), "table", TABLE_UUID)
    assert stored is not None and stored["retention_days"] == 7
    assert mp.get_policy(root, StorageOptions(), "table", "db$t") is None, "a table's record is keyed by its uuid, never its name"
    assert len(mp.list_policies(root, StorageOptions())) == 2

    assert mp.delete_policy(root, StorageOptions(), "table", TABLE_UUID) is True
    assert mp.delete_policy(root, StorageOptions(), "table", TABLE_UUID) is False  # idempotent
    assert len(mp.list_policies(root, StorageOptions())) == 1


def test_state_stamps_live_apart_from_policies(tmp_path: Path) -> None:
    # The sweep's cadence stamps must not appear in list_policies (separate writer, separate prefix).
    root = str(tmp_path)
    record = _policy(compact_interval_hours=6)
    uri = "s3://bkt/u1_db$t"
    mp.put_policy(root, StorageOptions(), record)
    assert mp.read_state(root, StorageOptions(), record, uri) is None
    mp.write_state(root, StorageOptions(), record, uri, "2026-07-16T00:00:00+00:00")
    assert mp.read_state(root, StorageOptions(), record, uri) == "2026-07-16T00:00:00+00:00"
    assert len(mp.list_policies(root, StorageOptions())) == 1


def test_state_stamps_are_per_dataset_not_per_policy(tmp_path: Path) -> None:
    # CONTRACT (audit 2026-07-16): a namespace policy covers many datasets, and a per-policy stamp let
    # the first dataset in discovery order starve every sibling (stamped fresh the same tick, siblings
    # skipped forever). One stamp per (policy, dataset).
    root = str(tmp_path)
    record = _policy(kind="namespace", id_="silver", uuid="ns-silver", compact_interval_hours=6)
    mp.write_state(root, StorageOptions(), record, "s3://bkt/medallion/silver/a", "2026-07-16T00:00:00+00:00")
    assert mp.read_state(root, StorageOptions(), record, "s3://bkt/medallion/silver/b") is None
    assert mp.read_state(root, StorageOptions(), record, "s3://bkt/medallion/silver/a") is not None


def test_list_policies_skips_a_corrupt_record(tmp_path: Path) -> None:
    # CONTRACT (audit 2026-07-16): one corrupt/malformed record must never void the others — the sweep
    # would otherwise maintain opted-out datasets and GC past protective retention.
    root = str(tmp_path)
    mp.put_policy(root, StorageOptions(), _policy(retention_days=365))
    (tmp_path / "_policies" / "table-corrupt.json").write_text("{truncated")
    (tmp_path / "_policies" / "table-nouuid.json").write_text('{"kind": "table"}')
    records = mp.list_policies(root, StorageOptions())
    assert len(records) == 1 and records[0]["retention_days"] == 365


def test_resolve_precedence_table_over_namespace_over_none() -> None:
    # The table's own record wins; else the NEAREST ancestor namespace's (ancestors outermost first); else none.
    records = [
        _policy(kind="namespace", id_="silver", uuid="ns-silver", retention_days=30),
        _policy(kind="namespace", id_="silver$deep", uuid="ns-deep", retention_days=10),
        _policy(id_="silver$features", uuid="t-features", retention_days=1),
    ]
    hit = mp.resolve_policy(records, "s3://bkt/t-features", table_uuid="t-features", namespace_uuids=["ns-silver", "ns-deep"])
    assert hit is not None and hit["retention_days"] == 1
    hit = mp.resolve_policy(records, "s3://bkt/t-other", table_uuid="t-other", namespace_uuids=["ns-silver", "ns-deep"])
    assert hit is not None and hit["retention_days"] == 10
    hit = mp.resolve_policy(records, "s3://bkt/t-other", table_uuid="t-other", namespace_uuids=["ns-silver"])
    assert hit is not None and hit["retention_days"] == 30
    assert mp.resolve_policy(records, "s3://bkt/t-elsewhere", table_uuid="t-elsewhere", namespace_uuids=["ns-gold"]) is None


def test_project_policy_round_trips_and_lists(tmp_path: Path) -> None:
    # CONTRACT (#84): a UUID-LESS project record (it matches by buckets, keyed by its id) must survive the
    # registry round-trip AND list_policies' validation — the sweep only ever sees what list returns.
    root = str(tmp_path)
    mp.put_policy(root, StorageOptions(), _project_policy(retention_days=90))
    stored = mp.get_policy(root, StorageOptions(), "project", "acme")
    assert stored is not None and stored["buckets"] == ["acme-wh"]
    listed = mp.list_policies(root, StorageOptions())
    assert len(listed) == 1 and listed[0]["kind"] == "project"
    assert mp.delete_policy(root, StorageOptions(), "project", "acme") is True
    assert mp.delete_policy(root, StorageOptions(), "project", "acme") is False  # idempotent


def test_list_policies_rejects_a_bucketless_project_record(tmp_path: Path) -> None:
    # A project record with no buckets could never match a dataset — malformed, skipped with a warning,
    # and it must not void the well-formed records next to it.
    root = str(tmp_path)
    mp.put_policy(root, StorageOptions(), _project_policy())
    mp.put_policy(root, StorageOptions(), {"kind": "project", "id": "ghost", "buckets": []})
    (tmp_path / "_policies" / "project-nolist.json").write_text('{"kind": "project", "id": "x"}')
    records = mp.list_policies(root, StorageOptions())
    assert len(records) == 1 and records[0]["id"] == "acme"


def test_resolve_precedence_table_beats_namespace_beats_project_beats_none() -> None:
    # CONTRACT (#84): resolution order is table -> nearest namespace -> project bucket -> None
    # (global defaults). The project tier is strictly the LAST fallback.
    records = [
        _policy(id_="silver$features", uuid="t-features", retention_days=1),
        _policy(kind="namespace", id_="silver", uuid="ns-silver", retention_days=30),
        _project_policy(buckets=["acme-wh", "acme-wh2"], retention_days=90),
    ]
    hit = mp.resolve_policy(records, "s3://acme-wh/t-features", table_uuid="t-features", namespace_uuids=["ns-silver"])
    assert hit is not None and hit["retention_days"] == 1  # table beats namespace beats project
    hit = mp.resolve_policy(records, "s3://acme-wh/t-other", table_uuid="t-other", namespace_uuids=["ns-silver"])
    assert hit is not None and hit["retention_days"] == 30  # namespace beats project
    hit = mp.resolve_policy(records, "s3://acme-wh/t-events", table_uuid="t-events", namespace_uuids=["ns-bronze"])
    assert hit is not None and hit["retention_days"] == 90  # project catches the rest of its buckets
    hit = mp.resolve_policy(records, "s3://acme-wh2/t-x", table_uuid="t-x", namespace_uuids=[])
    assert hit is not None and hit["retention_days"] == 90  # every bucket in the record is covered
    assert mp.resolve_policy(records, "s3://other-wh/t-x", table_uuid="t-x", namespace_uuids=[]) is None  # cross-bucket never matches
    # A bucket-NAME prefix must not match ("acme-wh2" covered explicitly above; "acme-whx" is not).
    assert mp.resolve_policy(records, "s3://acme-whx/t-x", table_uuid="t-x", namespace_uuids=[]) is None


def test_resolve_overlapping_project_claims_warns_and_matches_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Mallory layer 3 (audit 2026-07-23): two DISTINCT project records both claiming one bucket is a
    # contested registry — first-encountered-wins would let record ordering decide whose retention
    # policy destroys whose version history. The project tier must resolve to NO match, with a warning;
    # a table/namespace match above the tier, and a single uncontested claim, keep resolving.
    contested = [
        _project_policy(id_="acme", buckets=["shared-bkt"], retention_days=365),
        _project_policy(id_="evil", buckets=["shared-bkt", "evil-wh"], retention_days=1),
    ]
    with caplog.at_level("WARNING", logger="service_kit.lakehouse.maintenance_policies"):
        assert mp.resolve_policy(contested, "s3://shared-bkt/t", table_uuid="t", namespace_uuids=["ns-db"]) is None
    assert any(r.message == "maintenance_policy_project_overlap" for r in caplog.records)
    # The uncontested bucket in the same records still resolves its (single) claimant.
    hit = mp.resolve_policy(contested, "s3://evil-wh/t", table_uuid="t", namespace_uuids=["ns-db"])
    assert hit is not None and hit["id"] == "evil"
    # A namespace record shadows the project tier entirely — the overlap never reaches it.
    shadowed = [_policy(kind="namespace", id_="db", uuid="ns-db", retention_days=7), *contested]
    hit = mp.resolve_policy(shadowed, "s3://shared-bkt/t", table_uuid="t", namespace_uuids=["ns-db"])
    assert hit is not None and hit["retention_days"] == 7


def test_resolve_matches_a_table_by_its_uuid_never_by_its_name() -> None:
    # CONTRACT (ADR 0173 §8, [[LH-523]]): a record governs the object whose uuid it carries. A table created
    # later under the name a record was set for holds none of it, and a renamed table keeps its own.
    records = [_policy(id_="db1$users", uuid="t-old", retention_days=1)]
    assert mp.resolve_policy(records, "s3://bkt/t-new", table_uuid="t-new", namespace_uuids=["ns-db1"]) is None
    hit = mp.resolve_policy(records, "s3://bkt/t-old", table_uuid="t-old", namespace_uuids=["ns-elsewhere"])
    assert hit is not None and hit["retention_days"] == 1


def _settings(tmp_path: Path) -> MaintenanceSettings:
    return MaintenanceSettings.model_validate(
        {
            "s3_endpoint": "",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
            "policy_root": str(tmp_path),
            # The control root too: a unit test that reaches for the shipped default bucket is the defect.
            "control_root": str(tmp_path),
        }
    )


def test_the_cadence_skips_inside_the_interval(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    uri = "s3://bkt/u1_db$t"

    cadenced = _policy(compact_interval_hours=6)
    mp.put_policy(str(tmp_path), StorageOptions(), cadenced)
    # No stamp yet → maintain (an absent/lost stamp must never silence maintenance).
    assert cadence_skip(cadenced, settings=settings, uri=uri, now=now) is None
    # Fresh stamp → skip; stale stamp → maintain.
    mp.write_state(str(tmp_path), StorageOptions(), cadenced, uri, (now - timedelta(hours=1)).isoformat())
    assert cadence_skip(cadenced, settings=settings, uri=uri, now=now) == "policy_interval"
    mp.write_state(str(tmp_path), StorageOptions(), cadenced, uri, (now - timedelta(hours=7)).isoformat())
    assert cadence_skip(cadenced, settings=settings, uri=uri, now=now) is None


def test_a_naive_or_malformed_stamp_maintains_instead_of_raising(tmp_path: Path) -> None:
    # Audit 2026-07-16: `now` is timezone-aware, so aware-minus-naive raises TypeError — which must be
    # contained (fail toward maintaining), not abort the sweep at this dataset every tick forever.
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    uri = "s3://bkt/u1_db$t"
    cadenced = _policy(compact_interval_hours=6)
    mp.write_state(str(tmp_path), StorageOptions(), cadenced, uri, datetime.now().isoformat())
    assert cadence_skip(cadenced, settings=settings, uri=uri, now=now) is None
    mp.write_state(str(tmp_path), StorageOptions(), cadenced, uri, "not-a-timestamp")
    assert cadence_skip(cadenced, settings=settings, uri=uri, now=now) is None


def test_tag_pinned_version_survives_any_retention_policy(tmp_path: Path) -> None:
    # CONTRACT (the goal's protection clause): retention never removes the blessed model version or
    # anything a tag pins — Lance exempts tagged versions from cleanup. Driven with real Lance.
    uri = str(tmp_path / "ds")
    table = pa.table({"n": pa.array([1], pa.int64())})
    lance.write_dataset(table, uri, mode="overwrite")  # v1
    lance.write_dataset(table, uri, mode="append")  # v2
    lance.write_dataset(table, uri, mode="append")  # v3
    lance.dataset(uri).tags.create("blessed", 1)

    result = expire_versions(ExpiryTarget(uri=uri, storage_options={}), ExpiryPolicy(older_than=None, retain_versions=1))
    assert result.error is None, result.error

    ds = lance.dataset(uri)
    assert ds.count_rows() == 3  # the head is intact
    pinned = lance.dataset(uri, version=1)  # the tagged version is still checkout-able
    assert pinned.count_rows() == 1


def test_a_partial_policy_update_preserves_fields_it_did_not_mention(tmp_path: Any) -> None:
    """The destructive round-trip an audit found: `put_policy` overwrites the whole record, so a
    client that read a policy, changed one knob and sent it back wrote the model's DEFAULTS over
    every field it never mentioned — silently clearing `scan_batch_size` (the compaction memory
    bound, ~15 GB/thread without it on bronze page rows) and a `cleanup_enabled=False` legal hold.
    The lakehouse form does exactly that, and its own comment states the belief this makes true: an
    omitted knob means INHERIT."""
    from catalog.api.v1.endpoints.policies import _record
    from catalog.schemas import PolicyRequest

    stored = _record("table", "bronze$pages", PolicyRequest(scan_batch_size=64, cleanup_enabled=False), None, uuid=TABLE_UUID)
    assert stored["scan_batch_size"] == 64

    # A later save that only flips compaction off — exactly what the form sends.
    updated = _record("table", "bronze$pages", PolicyRequest(compact_enabled=False), stored, uuid=TABLE_UUID)

    assert updated["compact_enabled"] is False, "the field the caller DID set must apply"
    assert updated["scan_batch_size"] == 64, "the memory bound was silently cleared"
    assert updated["cleanup_enabled"] is False, "the legal hold was silently lifted"
