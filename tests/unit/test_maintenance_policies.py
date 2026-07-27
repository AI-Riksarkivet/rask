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
from common import maintenance_policies as mp
from compaction.core.config import CompactionSettings
from compaction.services.optimize import compact_one
from compaction.services.sweep import _policy_skip_reason


def _policy(kind: str = "table", id_: str = "db$t", path: str = "bkt/u1_db$t", **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "id": id_, "path": path, "compact_enabled": True, **fields}


def _project_policy(id_: str = "acme", buckets: list[str] | None = None, **fields: Any) -> dict[str, Any]:
    # #84 shape: no path — a project record matches by its warehouse BUCKETS (resolved at set time).
    return {
        "kind": "project",
        "id": id_,
        "path": "",
        "buckets": buckets if buckets is not None else ["acme-wh"],
        "compact_enabled": True,
        **fields,
    }


def test_registry_round_trips_and_lists(tmp_path: Path) -> None:
    root = str(tmp_path)
    mp.put_policy(root, {}, _policy(retention_days=7))
    mp.put_policy(root, {}, _policy(kind="namespace", id_="silver", path="bkt/medallion/silver"))

    stored = mp.get_policy(root, {}, "table", "db$t")
    assert stored is not None and stored["retention_days"] == 7
    assert mp.get_policy(root, {}, "table", "ghost") is None
    assert len(mp.list_policies(root, {})) == 2

    assert mp.delete_policy(root, {}, "table", "db$t") is True
    assert mp.delete_policy(root, {}, "table", "db$t") is False  # idempotent
    assert len(mp.list_policies(root, {})) == 1


def test_state_stamps_live_apart_from_policies(tmp_path: Path) -> None:
    # The sweep's cadence stamps must not appear in list_policies (separate writer, separate prefix).
    root = str(tmp_path)
    record = _policy(compact_interval_hours=6)
    uri = "s3://bkt/u1_db$t"
    mp.put_policy(root, {}, record)
    assert mp.read_state(root, {}, record, uri) is None
    mp.write_state(root, {}, record, uri, "2026-07-16T00:00:00+00:00")
    assert mp.read_state(root, {}, record, uri) == "2026-07-16T00:00:00+00:00"
    assert len(mp.list_policies(root, {})) == 1


def test_state_stamps_are_per_dataset_not_per_policy(tmp_path: Path) -> None:
    # CONTRACT (audit 2026-07-16): a namespace policy covers many datasets, and a per-policy stamp let
    # the first dataset in discovery order starve every sibling (stamped fresh the same tick, siblings
    # skipped forever). One stamp per (policy, dataset).
    root = str(tmp_path)
    record = _policy(kind="namespace", id_="silver", path="bkt/medallion/silver", compact_interval_hours=6)
    mp.write_state(root, {}, record, "s3://bkt/medallion/silver/a", "2026-07-16T00:00:00+00:00")
    assert mp.read_state(root, {}, record, "s3://bkt/medallion/silver/b") is None
    assert mp.read_state(root, {}, record, "s3://bkt/medallion/silver/a") is not None


def test_list_policies_skips_a_corrupt_record(tmp_path: Path) -> None:
    # CONTRACT (audit 2026-07-16): one corrupt/malformed record must never void the others — the sweep
    # would otherwise maintain opted-out datasets and GC past protective retention.
    root = str(tmp_path)
    mp.put_policy(root, {}, _policy(retention_days=365))
    (tmp_path / "_policies" / "table-corrupt.json").write_text("{truncated")
    (tmp_path / "_policies" / "table-nopath.json").write_text('{"kind": "table"}')
    records = mp.list_policies(root, {})
    assert len(records) == 1 and records[0]["retention_days"] == 365


def test_resolve_precedence_table_over_namespace_over_none() -> None:
    records = [
        _policy(kind="namespace", id_="silver", path="bkt/medallion/silver", retention_days=30),
        _policy(kind="namespace", id_="silver$deep", path="bkt/medallion/silver/deep", retention_days=10),
        _policy(id_="silver$features", path="bkt/medallion/silver/features", retention_days=1),
    ]
    # Exact table match wins.
    hit = mp.resolve_policy(records, "s3://bkt/medallion/silver/features")
    assert hit is not None and hit["retention_days"] == 1
    # Longest namespace prefix wins over a shorter one.
    hit = mp.resolve_policy(records, "s3://bkt/medallion/silver/deep/other")
    assert hit is not None and hit["retention_days"] == 10
    hit = mp.resolve_policy(records, "s3://bkt/medallion/silver/other")
    assert hit is not None and hit["retention_days"] == 30
    # A prefix must match on a path boundary, not substring ("silverware" is not under "silver").
    assert mp.resolve_policy(records, "s3://bkt/medallion/silverware/x") is None
    assert mp.resolve_policy(records, "s3://bkt/elsewhere/tbl") is None
    # Bucket-qualified: the same path in another tenant's bucket must never cross-match.
    assert mp.resolve_policy(records, "s3://otherbkt/medallion/silver/features") is None


def test_project_policy_round_trips_and_lists(tmp_path: Path) -> None:
    # CONTRACT (#84): a PATH-LESS project record (it matches by buckets, not path) must survive the
    # registry round-trip AND list_policies' validation — the sweep only ever sees what list returns.
    root = str(tmp_path)
    mp.put_policy(root, {}, _project_policy(retention_days=90))
    stored = mp.get_policy(root, {}, "project", "acme")
    assert stored is not None and stored["buckets"] == ["acme-wh"]
    listed = mp.list_policies(root, {})
    assert len(listed) == 1 and listed[0]["kind"] == "project"
    assert mp.delete_policy(root, {}, "project", "acme") is True
    assert mp.delete_policy(root, {}, "project", "acme") is False  # idempotent


def test_list_policies_rejects_a_bucketless_project_record(tmp_path: Path) -> None:
    # A project record with no buckets could never match a dataset — malformed, skipped with a warning,
    # and it must not void the well-formed records next to it.
    root = str(tmp_path)
    mp.put_policy(root, {}, _project_policy())
    mp.put_policy(root, {}, {"kind": "project", "id": "ghost", "path": "", "buckets": []})
    (tmp_path / "_policies" / "project-nolist.json").write_text('{"kind": "project", "id": "x"}')
    records = mp.list_policies(root, {})
    assert len(records) == 1 and records[0]["id"] == "acme"


def test_resolve_precedence_table_beats_namespace_beats_project_beats_none() -> None:
    # CONTRACT (#84): resolution order is exact table -> longest namespace -> project bucket -> None
    # (global defaults). The project tier is strictly the LAST fallback.
    records = [
        _policy(id_="silver$features", path="acme-wh/medallion/silver/features", retention_days=1),
        _policy(kind="namespace", id_="silver", path="acme-wh/medallion/silver", retention_days=30),
        _project_policy(buckets=["acme-wh", "acme-wh2"], retention_days=90),
    ]
    hit = mp.resolve_policy(records, "s3://acme-wh/medallion/silver/features")
    assert hit is not None and hit["retention_days"] == 1  # table beats namespace beats project
    hit = mp.resolve_policy(records, "s3://acme-wh/medallion/silver/other")
    assert hit is not None and hit["retention_days"] == 30  # namespace beats project
    hit = mp.resolve_policy(records, "s3://acme-wh/medallion/bronze/events")
    assert hit is not None and hit["retention_days"] == 90  # project catches the rest of its buckets
    hit = mp.resolve_policy(records, "s3://acme-wh2/u1_db$t")
    assert hit is not None and hit["retention_days"] == 90  # every bucket in the record is covered
    assert mp.resolve_policy(records, "s3://other-wh/u1_db$t") is None  # cross-bucket never matches
    # A bucket-NAME prefix must not match ("acme-wh2" covered explicitly above; "acme-whx" is not).
    assert mp.resolve_policy(records, "s3://acme-whx/u1_db$t") is None


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
    with caplog.at_level("WARNING", logger="common.maintenance_policies"):
        assert mp.resolve_policy(contested, "s3://shared-bkt/u1_db$t") is None
    assert any(r.message == "maintenance_policy_project_overlap" for r in caplog.records)
    # The uncontested bucket in the same records still resolves its (single) claimant.
    hit = mp.resolve_policy(contested, "s3://evil-wh/u1_db$t")
    assert hit is not None and hit["id"] == "evil"
    # A namespace record shadows the project tier entirely — the overlap never reaches it.
    shadowed = [_policy(kind="namespace", id_="db", path="shared-bkt/root/db", retention_days=7), *contested]
    hit = mp.resolve_policy(shadowed, "s3://shared-bkt/root/db/t")
    assert hit is not None and hit["retention_days"] == 7


def test_project_policy_matches_flat_catalog_layout_without_a_logical_id() -> None:
    # The bucket-level match needs no logical id, so BOTH the catalog's flat `<uuid>_<table_id>` layout
    # and the medallion-nested layout in a project bucket resolve the project record.
    records = [_project_policy(retention_days=90)]
    for uri in ("s3://acme-wh/u1_db$users", "s3://acme-wh/medallion/gold/features"):
        hit = mp.resolve_policy(records, uri, logical_id=None)
        assert hit is not None and hit["retention_days"] == 90, uri


def test_sweep_consumes_a_project_policy_via_the_same_resolution_call(tmp_path: Path) -> None:
    # PIN (#84): the sweep needs NO change to honor the project tier — run_sweep resolves via
    # list_policies + resolve_policy(records, uri, logical_id=table_id_from_uri(uri)) and then applies
    # the skip logic. Drive exactly that call shape off a real registry and prove a project record
    # both resolves and opts its bucket's datasets out.
    from compaction.core.lineage_emit import table_id_from_uri

    root = str(tmp_path)
    mp.put_policy(root, {}, _project_policy(compact_enabled=False))
    records = mp.list_policies(root, {})
    settings = _settings(tmp_path)
    now = datetime.now(UTC)

    uri = "s3://acme-wh/medallion/silver/events"  # medallion-nested: table_id_from_uri yields no id
    policy = mp.resolve_policy(records, uri, logical_id=table_id_from_uri(uri), delimiter=settings.delimiter)
    assert policy is not None and policy["kind"] == "project"
    assert _policy_skip_reason(policy, settings, {}, now, uri) == "policy_disabled"

    outside = "s3://other-wh/u1_db$t"
    assert mp.resolve_policy(records, outside, logical_id=table_id_from_uri(outside)) is None


def test_resolve_matches_namespace_by_logical_parent_chain() -> None:
    # CONTRACT (audit 2026-07-16): the catalog lays tables out flat (`<uuid>_<table_id>`), never under a
    # namespace directory — so a namespace policy must also match via the dataset's logical parent chain
    # or it would be a dead letter for every catalog-created table.
    records = [
        _policy(kind="namespace", id_="db1", path="bkt/root/db1", retention_days=30),
        _policy(kind="namespace", id_="db1$sub", path="bkt/root/db1/sub", retention_days=10),
    ]
    uri = "s3://bkt/root/u1_db1$users"
    assert mp.resolve_policy(records, uri) is None  # the physical prefix alone never matches
    hit = mp.resolve_policy(records, uri, logical_id="db1$users")
    assert hit is not None and hit["retention_days"] == 30
    # The deepest namespace on the chain wins, and an exact table policy wins over both.
    hit = mp.resolve_policy(records, "s3://bkt/root/u2_db1$sub$t", logical_id="db1$sub$t")
    assert hit is not None and hit["retention_days"] == 10
    records.append(_policy(id_="db1$users", path="bkt/root/u1_db1$users", retention_days=1))
    hit = mp.resolve_policy(records, uri, logical_id="db1$users")
    assert hit is not None and hit["retention_days"] == 1


def _settings(tmp_path: Path) -> CompactionSettings:
    return CompactionSettings.model_validate(
        {
            "s3_endpoint": "",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
            "policy_root": str(tmp_path),
        }
    )


def test_skip_reasons_disabled_and_interval(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    uri = "s3://bkt/u1_db$t"

    assert _policy_skip_reason(_policy(compact_enabled=False), settings, {}, now, uri) == "policy_disabled"

    cadenced = _policy(compact_interval_hours=6)
    mp.put_policy(str(tmp_path), {}, cadenced)
    # No stamp yet → maintain (an absent/lost stamp must never silence maintenance).
    assert _policy_skip_reason(cadenced, settings, {}, now, uri) is None
    # Fresh stamp → skip; stale stamp → maintain.
    mp.write_state(str(tmp_path), {}, cadenced, uri, (now - timedelta(hours=1)).isoformat())
    assert _policy_skip_reason(cadenced, settings, {}, now, uri) == "policy_interval"
    mp.write_state(str(tmp_path), {}, cadenced, uri, (now - timedelta(hours=7)).isoformat())
    assert _policy_skip_reason(cadenced, settings, {}, now, uri) is None


def test_a_naive_or_malformed_stamp_maintains_instead_of_raising(tmp_path: Path) -> None:
    # Audit 2026-07-16: `now` is timezone-aware, so aware-minus-naive raises TypeError — which must be
    # contained (fail toward maintaining), not abort the sweep at this dataset every tick forever.
    settings = _settings(tmp_path)
    now = datetime.now(UTC)
    uri = "s3://bkt/u1_db$t"
    cadenced = _policy(compact_interval_hours=6)
    mp.write_state(str(tmp_path), {}, cadenced, uri, datetime.now().isoformat())
    assert _policy_skip_reason(cadenced, settings, {}, now, uri) is None
    mp.write_state(str(tmp_path), {}, cadenced, uri, "not-a-timestamp")
    assert _policy_skip_reason(cadenced, settings, {}, now, uri) is None


def test_tag_pinned_version_survives_any_retention_policy(tmp_path: Path) -> None:
    # CONTRACT (the goal's protection clause): retention never removes the blessed model version or
    # anything a tag pins — Lance exempts tagged versions from cleanup. Driven with real Lance.
    uri = str(tmp_path / "ds")
    table = pa.table({"n": pa.array([1], pa.int64())})
    lance.write_dataset(table, uri, mode="overwrite")  # v1
    lance.write_dataset(table, uri, mode="append")  # v2
    lance.write_dataset(table, uri, mode="append")  # v3
    lance.dataset(uri).tags.create("blessed", 1)

    result = compact_one(uri, {}, older_than=None, retain_versions=1)
    assert result.error is None, result.error

    ds = lance.dataset(uri)
    assert ds.count_rows() == 3  # the head is intact
    pinned = lance.dataset(uri, version=1)  # the tagged version is still checkout-able
    assert pinned.count_rows() == 1


def test_retention_policy_prunes_unpinned_old_versions(tmp_path: Path) -> None:
    # The observable flip side: with retain_versions=1 + older_than=0, an untagged old version is gone.
    uri = str(tmp_path / "ds")
    table = pa.table({"n": pa.array([1], pa.int64())})
    lance.write_dataset(table, uri, mode="overwrite")  # v1 (untagged)
    lance.write_dataset(table, uri, mode="append")  # v2
    lance.write_dataset(table, uri, mode="append")  # v3

    result = compact_one(uri, {}, older_than=None, retain_versions=1)
    assert result.error is None, result.error
    assert result.old_versions_removed >= 1

    with pytest.raises((ValueError, OSError)):
        lance.dataset(uri, version=1)  # pruned per policy
