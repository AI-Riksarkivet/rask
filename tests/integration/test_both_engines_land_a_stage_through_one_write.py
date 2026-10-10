"""Both cascade engines land a stage through ONE write, `service_kit.lakehouse.tier_write` (CP-056).

Every write-semantics claim below runs on the in-process engine (`medallion.services.compute.transform_stage`) and on
the Ray stage job (`scripts/ray_stage_job.py::_run_stage`, loaded by path), over real pylance on `tmp_path`. A claim
that holds on one lane and not the other is the drift the shared module exists to make impossible, so each test is
parametrized over both rather than written twice. Ray's distributed producer (`lance_ray` on a Ray cluster) is not
started here: the head, delta and media producers are the ones these claims reach, and the distributed producer's own
tests live in `tests/unit/test_ray_stage_job.py`.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import lance
import pyarrow as pa
import pytest
from lance import blob_array, blob_field
from lance.blob import Blob

from lineage_kit.consume import DatasetRef, LineageDoc
from lineage_kit.schemas import JobRef
from medallion.services.compute import seed_bronze, transform_stage
from service_kit.lakehouse import blobs, tier_write
from service_kit.lakehouse.commit_marker import CommitMarker, marked_version
from service_kit.lakehouse.stage_stamp import LINEAGE_DATASET_ID_KEY, UnstableRowIdsError, stamp_stage


_JOB_PATH = Path(__file__).parents[2] / "scripts" / "ray_stage_job.py"


def _load_job() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ray_stage_job_one_write", _JOB_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


job = _load_job()

LANES = [pytest.param("inprocess", id="inprocess"), pytest.param("ray", id="ray")]


def _doc(run_id: str, stage: str = "silver") -> LineageDoc:
    return LineageDoc(
        run_id=run_id,
        job=JobRef(namespace="medallion", name=stage),
        event_time="2026-10-05T00:00:00Z",
        event_type="COMPLETE",
        producer="https://example.invalid/rask",
        output=DatasetRef(namespace="acme-silver", name="events"),
    )


def _run(
    lane: str,
    from_uri: str,
    to_uri: str,
    *,
    stage: str = "silver",
    lineage: LineageDoc | None = None,
    version_floor: int | None = None,
    dataset_id: str = "",
    marker: CommitMarker | None = None,
) -> int | None:
    """One stage run on ``lane``, as its engine is driven in production; answers the version the run names."""
    if lane == "inprocess":
        return transform_stage(
            from_uri, to_uri, {}, stage=stage, lineage=lineage, dataset_id=dataset_id or None, version_floor=version_floor, marker=marker
        ).version
    return job._run_stage(
        from_uri, to_uri, stage, {}, lineage=lineage.to_json() if lineage else "", base_version=version_floor, dataset_id=dataset_id, marker=marker
    )


def _by_id(uri: str, column: str, version: int | None = None) -> dict[int, Any]:
    table = lance.dataset(uri, version=version).to_table(columns=["id", column])
    return dict(zip(table.column("id").to_pylist(), table.column(column).to_pylist(), strict=True))


def _versions(uri: str) -> set[int]:
    return {int(v["version"]) for v in lance.dataset(uri).versions()}


#: What a tier armed out of band carries ([[LH-245]]): one commit on it would delete every older version.
_ARMED: dict[str, str | None] = {"lance.auto_cleanup.interval": "1", "lance.auto_cleanup.older_than": "0s", "lance.auto_cleanup.retain_versions": "1"}


# --- LH-216: provenance needs stable row ids, and a delta follows Lance's own deletion record --------------------------


@pytest.mark.parametrize("payload", ["tabular", "managed-blob"])
@pytest.mark.parametrize("lane", LANES)
def test_a_head_hop_refuses_an_upstream_without_stable_row_ids(tmp_path: Path, lane: str, payload: str) -> None:
    """`source_rowid` minted from a non-stable `_rowid` names a physical address the next compaction rewrites.

    Stable row ids are create-time-only (`lance_docs/file_format.md:4011-4015`), so the head hop refuses rather than
    writing a tier whose every parent reference goes stale on the first maintenance pass.
    """
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    tabular = payload == "tabular"
    field = pa.field("payload", pa.string()) if tabular else blob_field("payload")
    column = pa.array(["a", "b"]) if tabular else blob_array([b"a", b"b"])
    lance.write_dataset(pa.table({"id": pa.array([0, 1], pa.int64()), "payload": column}, schema=pa.schema([pa.field("id", pa.int64()), field])), bronze)

    with pytest.raises(UnstableRowIdsError, match="stable row ids"):
        _run(lane, bronze, silver)

    assert not Path(silver).exists(), "a refused head hop must write no tier"


@pytest.mark.parametrize("hop", ["head", "deeper"])
@pytest.mark.parametrize("lane", LANES)
def test_a_delta_run_converges_only_its_window_and_retracts_what_lance_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str, hop: str) -> None:
    """One window holding an in-place update, a delete and a compaction, over a tier armed out of band.

    The tier loses exactly the deleted row and takes exactly the corrected one: a row outside the window keeps the
    stage label of the run that wrote it. The deleted set is Lance's record (`delta(...).get_deleted_row_ids()`), so
    no key column is read whole on either side, and a compaction (which deletes no row) retracts nothing. At the head
    the deleted `_rowid`s are the keys; deeper in they are mapped to `source_rowid` through the upstream at the
    boundary. The run disarms the tier before its first commit and keeps every version it had ([[LH-245]]), and its
    marker sits on the version it reports.
    """
    upstream = str(tmp_path / "bronze.lance")
    seed_bronze(upstream, {}, rows=4)
    tier = str(tmp_path / "tier.lance")
    if hop == "deeper":
        silver = str(tmp_path / "silver.lance")
        _run(lane, upstream, silver, lineage=_doc("r-silver"))
        upstream = silver
        lance.write_dataset(lance.dataset(upstream).to_table(), tier, data_storage_version="2.2", enable_stable_row_ids=True)
    else:
        _run(lane, upstream, tier, stage="first-pass", lineage=_doc("r-full"))
    if hop == "deeper":
        lance.dataset(tier).update({"stage": "'first-pass'"})
    boundary = lance.dataset(upstream).version
    lance.dataset(upstream).update({"payload": "'corrected'"}, where="id = 3")
    lance.dataset(upstream).delete("id = 1")
    lance.dataset(upstream).optimize.compact_files(target_rows_per_fragment=1024)
    lance.dataset(tier).update_config(_ARMED)
    kept = _versions(tier)

    scans: list[tuple[tuple[str, ...], str | None]] = []
    real_scanner = lance.LanceDataset.scanner

    def recording_scanner(self: lance.LanceDataset, *args: Any, **kwargs: Any) -> lance.LanceScanner:
        columns = kwargs.get("columns")
        scans.append((tuple(columns) if isinstance(columns, list) else (), cast("str | None", kwargs.get("filter"))))
        return real_scanner(self, *args, **kwargs)

    monkeypatch.setattr(lance.LanceDataset, "scanner", recording_scanner)
    marker = CommitMarker(action_id="delta-run", run_id="r-delta")
    version = _run(lane, upstream, tier, stage="backfill-pass", lineage=_doc("r-delta"), version_floor=boundary, marker=marker)

    assert _by_id(tier, "payload") == {0: "event-0", 2: "event-2", 3: "corrected"}, "the tier did not converge on exactly the window"
    assert _by_id(tier, "stage") == {0: "first-pass", 2: "first-pass", 3: "backfill-pass"}, "the run rewrote rows outside its window"
    whole_key_reads = [scan for scan in scans if scan[1] is None and {"_rowid", "source_rowid"} & set(scan[0])]
    assert whole_key_reads == [], f"the retraction read a whole key column: {whole_key_reads}"
    assert kept <= _versions(tier), "the stage run deleted the tier's versions"
    assert version == marked_version(tier, {}, action_id="delta-run", above=None), "the run's marker is not on the version it reports"


# --- LH-217: a carried blob column keeps every payload and its field metadata ------------------------------------------


#: Thresholds low enough that one small column holds every managed placement Blob V2 has: inline (kind 0) below
#: 100 B, packed (kind 1) up to 5,000 B, dedicated (kind 2) above it.
_THRESHOLDS = {b"lance-encoding:blob-inline-size-threshold": b"100", b"lance-encoding:blob-dedicated-size-threshold": b"5000"}
_CLASSIFICATION = "rask.classification"


def _mixed_bronze(uri: str, external: str | None, base: str | None) -> list[bytes | None]:
    """A bronze whose blob field carries thresholds and every payload kind; answers the bytes a reader sees, by id."""
    field = blob_field("payload", nullable=True).with_metadata(_THRESHOLDS)
    managed: list[bytes | None] = [b"i" * 40, b"p" * 1_000, b"d" * 9_000, None]
    values: list[Any] = ([Blob.from_uri(external)] if external else []) + managed
    lance.write_dataset(
        pa.table({"id": pa.array(range(len(values)), pa.int64()), "payload": blob_array(values)}, schema=pa.schema([pa.field("id", pa.int64()), field])),
        uri,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        initial_bases=[lance.DatasetBasePath(base, "source")] if base else None,
    )
    return ([Path(external[7:]).read_bytes()] if external else []) + managed


@pytest.mark.parametrize("placement", ["external", "managed"])
@pytest.mark.parametrize("lane", LANES)
def test_a_carried_blob_column_keeps_every_payload_and_its_field_metadata(tmp_path: Path, lane: str, placement: str) -> None:
    """An external base does not make every row external, and a label set after the tier exists still reaches it.

    Kind-3 rows are forwarded as pointers and inline, packed and dedicated rows keep their bytes ([[LH-217]]). The tier
    is created under the upstream's own blob field (thresholds included), and a re-run copies a `rask.*` label set on
    bronze after silver exists, add-only: silver's own classification on `id` is not replaced by bronze's.
    """
    source = tmp_path / "corpus"
    source.mkdir()
    page = source / "page-000.bin"
    page.write_bytes(b"P" * 2_000)
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    external = placement == "external"
    expected = _mixed_bronze(bronze, page.resolve().as_uri() if external else None, base=str(source) if external else None)

    _run(lane, bronze, silver)
    lance.dataset(silver).update_field_metadata({"id": {_CLASSIFICATION: "secret"}})
    lance.dataset(bronze).update_field_metadata({"payload": {_CLASSIFICATION: "restricted"}, "id": {_CLASSIFICATION: "internal"}})
    _run(lane, bronze, silver)

    out = lance.dataset(silver)
    assert out.scanner(columns=["id", "payload"], blob_handling="all_binary").to_table().sort_by("id").column("payload").to_pylist() == expected
    if external:
        kinds = out.to_table(columns=["id", "payload"]).sort_by("id").column("payload").to_pylist()
        assert kinds[0]["kind"] == blobs.EXTERNAL_KIND, "the external row was copied rather than forwarded"
    assert out.schema.field("payload").metadata == {**_THRESHOLDS, _CLASSIFICATION.encode(): b"restricted"}
    assert (out.schema.field("id").metadata or {}).get(_CLASSIFICATION.encode()) == b"secret", "the carry replaced the tier's own classification"


# --- LH-213 and LH-245 on the full lane: a re-run writes what its upstream holds now ----------------------------------


@pytest.mark.parametrize("armed", [pytest.param(False, id="plain"), pytest.param(True, id="armed-tier")])
@pytest.mark.parametrize("lane", LANES)
def test_a_payload_corrected_in_place_reaches_silver_under_the_run_that_carried_it(tmp_path: Path, lane: str, armed: bool) -> None:
    """A corrected row keeps its stable `_rowid` and its position, so only a write of what the upstream holds NOW carries it.

    The re-run re-stamps every row's `lineage` with its own document, keeps the stamp's column order (`stage` re-stamped
    in place), corrects the tier's declared dataset id while keeping every other schema-metadata key, and on a tier
    armed out of band disarms it before its first commit and keeps every version silver had ([[LH-245]]).
    """
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    seed_bronze(bronze, {}, rows=3)
    _run(lane, bronze, silver, lineage=_doc("r-first"), dataset_id="acme$bronze")
    lance.dataset(silver).update_schema_metadata({"description": "a table someone described"})
    lance.dataset(bronze).update({"payload": "'event-2-corrected'"}, where="id = 2")
    if armed:
        lance.dataset(silver).update_config(_ARMED)
    kept = _versions(silver)

    _run(lane, bronze, silver, lineage=_doc("r-second"), dataset_id="acme$silver")

    out = lance.dataset(silver)
    assert _by_id(silver, "payload") == {0: "event-0", 1: "event-1", 2: "event-2-corrected"}
    assert {json.loads(cell)["run_id"] for cell in _by_id(silver, "lineage").values()} == {"r-second"}
    assert out.schema.names == ["id", "payload", "stage", "source_rowid", "lineage"]
    metadata = {k.decode(): v.decode() for k, v in (out.schema.metadata or {}).items()}
    assert metadata[LINEAGE_DATASET_ID_KEY] == "acme$silver" and metadata["description"] == "a table someone described"
    assert kept <= _versions(silver), "the stage run deleted silver's versions"


@pytest.mark.parametrize("lane", LANES)
def test_a_new_column_lands_on_its_own_rows_when_the_tier_moves_under_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    """The upstream gains a per-row column while silver's rows have moved, and move again mid-write.

    The column is joined on `id` through the handle that read the schema, and Lance refuses that Merge once another
    commit passed it, so the tier is re-read. Every version that holds the column is read, not only the last.
    """
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    seed_bronze(bronze, {}, rows=3)
    _run(lane, bronze, silver, lineage=_doc("r-first"))
    lance.dataset(bronze).add_columns({"score": "id * 10"})
    # An update rewrites the row at the end of the tier: silver's physical order is now 1, 2, 0.
    lance.dataset(silver).update({"stage": "'silver'"}, where="id = 0")

    real_merge: Callable[..., Any] = lance.LanceDataset.merge
    moved: list[int] = []

    def merge_after_a_concurrent_commit(self: lance.LanceDataset, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if not moved:
            moved.append(1)
            lance.dataset(silver).update({"stage": "'silver'"}, where="id = 1")
        return real_merge(self, *args, **kwargs)

    monkeypatch.setattr(lance.LanceDataset, "merge", merge_after_a_concurrent_commit)
    _run(lane, bronze, silver, lineage=_doc("r-second"))

    holding = [v for v in sorted(_versions(silver)) if "score" in lance.dataset(silver, version=v).schema.names]
    assert holding, "the run never added the column"
    assert {version: _by_id(silver, "score", version) for version in holding} == {version: {0: 0, 1: 10, 2: 20} for version in holding}


# --- CP-029 D-5: the run's marker sits on its one data commit ---------------------------------------------------------


@pytest.mark.parametrize("lane", LANES)
def test_a_marked_run_that_loses_a_commit_race_lands_on_the_version_it_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    """A redelivery races a concurrent marked run, which commits between the redelivery's plan and its commit.

    Both land, the redelivery re-plans onto the newer version, and its marker sits on the version the run reports, which
    is the data commit and not the lineage index built after it: the WROTE edge and the next tier's version range name
    rows, never an index (measured 2026-09-11: 253 of 253 stage-authored producer edges on a `CreateIndex` version).
    """
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    seed_bronze(bronze, {}, rows=3)
    doc = _doc("r-1")
    _run(lane, bronze, silver, lineage=doc)
    before = lance.dataset(silver).version

    real_stamped = tier_write.stamped
    raced: list[str] = []

    def a_concurrent_run_commits_first(transaction: lance.Transaction, marker: CommitMarker) -> lance.Transaction:
        if not raced:
            raced.append(marker.action_id)
            rows = stamp_stage(lance.dataset(bronze).to_table(with_row_id=True), stage="silver", stable_row_ids=True, lineage=doc.to_json())
            concurrent = tier_write.TierTarget(uri=silver, storage_options={}, marker=CommitMarker(action_id="concurrent-run"))
            tier_write.write_tier(lance.dataset(bronze), rows, tier_write.Window(), concurrent)
        return real_stamped(transaction, marker)

    monkeypatch.setattr(tier_write, "stamped", a_concurrent_run_commits_first)
    reported = _run(lane, bronze, silver, lineage=doc, marker=CommitMarker(action_id="redelivery", run_id="r-1"))

    assert lance.dataset(silver).count_rows() == 3, "the redelivery duplicated rows"
    assert raced == ["redelivery"], "the concurrent run never raced the redelivery's commit"
    landed = marked_version(silver, {}, action_id="concurrent-run", above=before)
    assert landed is not None and landed > before, "the concurrent run's merge did not land"
    assert reported == marked_version(silver, {}, action_id="redelivery", above=landed), "the redelivery's marker is not on the version it reports"
    assert lance.dataset(silver).version > cast("int", reported), "this fixture must build the index after the data commit, or it proves nothing"


# --- LH-326: the media lane converges in ONE commit and keeps row identity --------------------------------------------


def _png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (24, 18), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _media_bronze(uri: str, ids: list[int], mode: str = "create") -> None:
    lance.write_dataset(
        pa.table(
            {"id": pa.array(ids, pa.int64()), "payload": blob_array([_png((10 * i % 255, 40, 90)) for i in ids])},
            schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
        ),
        uri,
        mode=mode,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )


@pytest.mark.parametrize("lane", LANES)
def test_a_media_rerun_retracts_and_ends_on_its_marked_commit_keeping_the_survivors_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    """A re-run whose source dropped a row retracts it, re-mints no survivor's `_rowid`, and ends on its marked commit.

    The tier above stores this tier's stable `_rowid` as its `source_rowid`, so re-minting a survivor detaches its
    children. Both engines stream one row per slice here, and the slices are gathered into commit units of
    `tier_write.STREAM_COMMIT_BYTES`: every unit but the last lands as an upsert, the retraction follows, and the last
    unit carries the marker and is the version the run reports; only the lineage index follows it. A commit per slice
    would make a re-run of N rows cost N commits, each joined against the whole tier ([[CP-051]]).
    """
    from medallion.core.config import MedallionSettings
    from medallion.services import compute

    monkeypatch.setattr(job, "MEDIA_BATCH_ROWS", 1)
    monkeypatch.setattr(compute, "get_settings", lambda: MedallionSettings(stage_batch_rows=1))
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    ids = list(range(10, 50))
    _media_bronze(bronze, ids)
    _run(lane, bronze, silver, lineage=_doc("run-one"))
    first = _by_id(silver, "source_rowid")
    rowids = {r["id"]: r["_rowid"] for r in lance.dataset(silver).to_table(columns=["id"], with_row_id=True).to_pylist()}
    lance.dataset(bronze).delete("id = 11")
    payload_bytes = sum(len(p) for p in blobs.read_aligned_table(lance.dataset(bronze), columns=["payload"]).column("payload").to_pylist())
    before = lance.dataset(silver).version

    reported = _run(lane, bronze, silver, lineage=_doc("run-two"), marker=CommitMarker(action_id="media-rerun"))

    survivors = [i for i in ids if i != 11]
    after = {r["id"]: r["_rowid"] for r in lance.dataset(silver).to_table(columns=["id"], with_row_id=True).to_pylist()}
    assert after == {i: rowids[i] for i in survivors}, f"the rerun did not retract 11 or re-minted a survivor: {rowids} -> {after}"
    assert _by_id(silver, "source_rowid") == {i: first[i] for i in survivors}
    assert reported == marked_version(silver, {}, action_id="media-rerun", above=before), "the run's marker is not on the version it reports"
    assert max(_versions(silver)) == cast("int", reported) + 1, "a commit of the run landed after its marked commit, other than the lineage index"
    units = -(-payload_bytes // tier_write.STREAM_COMMIT_BYTES)
    assert max(_versions(silver)) - before <= units + 2, (
        f"the rerun of {len(survivors)} one-row slices ({payload_bytes} B) added {max(_versions(silver)) - before} versions; "
        f"{units} commit unit(s), the retraction and the lineage index are {units + 2}"
    )


#: One stage write in a FRESH process, so its VmHWM is the write's own peak and nothing an earlier test allocated. It
#: prints the VmHWM after every import (the mark) and after the write. ``ray`` and ``inprocess`` are each engine's real
#: path; ``control`` lands the same stream as ONE whole-tier merge.
_PEAK_SCRIPT = """
import importlib.util, sys
import lance
mode, job_path, bronze, silver = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("job", job_path)
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)
from service_kit.lakehouse.commit_marker import CommitMarker
from medallion.services.compute import transform_stage
def hwm():
    return next(line for line in open("/proc/self/status") if line.startswith("VmHWM")).split()[1]
mark = hwm()
if mode == "ray":
    job._run_stage(bronze, silver, "silver", {}, marker=CommitMarker(action_id="peak"))
elif mode == "inprocess":
    transform_stage(bronze, silver, {}, stage="silver", marker=CommitMarker(action_id="peak"))
else:
    rows = job._media_rows(lance.dataset(bronze), stage="silver", lineage="", dataset_id="")
    tier = lance.dataset(silver)
    tier.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().when_not_matched_by_source_delete().execute_uncommitted(rows())
print(mark, hwm())
"""

#: What a stage may add to a stage runner: its 512 Mi limit (`resources.default`) less the ~210 Mi it idles at
#: (`kubectl top`, 2026-10-10).
_STAGE_RUNNER_HEADROOM_MB = 512 - 210


def _peak_mb(mode: str, bronze: str, silver: str) -> tuple[int, int]:
    """``(peak, growth)`` in MB: the run's VmHWM, and how far it rose above the mark taken after every import."""
    # The allocator bound every lakehouse pod carries (`lance.allocatorEnv`): without it glibc keeps one arena per host
    # core and RSS settles at the sum of their high-water marks, which measures the allocator rather than the write.
    # The in-process engine runs at its deployed defaults; the Ray lane at 16-row slices.
    env = {**os.environ, "RASK_STAGE_MEDIA_BATCH_ROWS": "16", "MALLOC_ARENA_MAX": "2", "ARROW_DEFAULT_MEMORY_POOL": "system", "PYTHONWARNINGS": "ignore"}
    for name in ("MEDALLION_STAGE_BATCH_ROWS", "MEDALLION_STAGE_IO_BUFFER_MB", "MEDALLION_STAGE_COMMIT_MB"):
        env.pop(name, None)
    out = subprocess.run([sys.executable, "-c", _PEAK_SCRIPT, mode, str(_JOB_PATH), bronze, silver], capture_output=True, text=True, env=env, check=True)
    mark, peak = (int(value) // 1024 for value in out.stdout.split()[-2:])
    return peak, peak - mark


@pytest.mark.parametrize("lane", LANES)
def test_a_media_converge_holds_a_slice_not_the_tier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str) -> None:
    """A media re-run over a 512 MiB tier (512 blob rows of 1 MiB) is sized by its slice, not by the tier.

    The whole-tier control grows with the tier and a sliced converge does not, so the tier is large enough that the
    ratio is not a coin toss. Measured on pylance 12.0.0 with the lakehouse pods' allocator bound: the Ray lane in
    16-row slices peaked at 0.48 and 0.51 GB VmHWM over 256 and 800 MiB tiers, where one whole-tier merge of the same
    stream peaked at 1.23 and 2.74 GB. The in-process engine runs in the stage runner, so at its defaults its growth
    over the post-import mark must also fit what that pod has left ([[CP-051]]). Each measurement runs in its own
    process against the same tier, so the write's shape is the only variable.
    """
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload")])
    slices = (
        pa.record_batch([pa.array(range(s, s + 32), pa.int64()), blob_array([os.urandom(1 << 20) for _ in range(32)])], schema=schema)
        for s in range(0, 512, 32)
    )
    lance.write_dataset(pa.RecordBatchReader.from_batches(schema, slices), bronze, data_storage_version="2.2", enable_stable_row_ids=True)
    monkeypatch.setattr(job, "MEDIA_BATCH_ROWS", 16)
    job._run_stage(bronze, silver, "silver", {})

    bounded, growth = _peak_mb(lane, bronze, silver)
    control, _ = _peak_mb("control", bronze, silver)

    assert bounded * 2 < control, f"the {lane} media converge peaked at {bounded} MB, against {control} MB for one whole-tier merge of the same stream"
    if lane == "inprocess":
        assert growth < _STAGE_RUNNER_HEADROOM_MB, f"the in-process stage grew {growth} MB, past the {_STAGE_RUNNER_HEADROOM_MB} MB a stage runner has left"


@pytest.mark.parametrize("lane", LANES)
def test_a_null_page_is_carried_through_on_its_own_row(tmp_path: Path, lane: str) -> None:
    """A missing page keeps its row: a null payload with null artifacts, every tabular column on its own row (R27).

    The tier's column order is the stamp's on both lanes: carried columns in upstream order, then root provenance,
    the stage, and the derived artifacts.
    """
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("page_key", pa.string())])
    lance.write_dataset(
        pa.table(
            {"id": pa.array([0, 1, 2], pa.int64()), "payload": blob_array([_png((200, 30, 30)), None, _png((30, 30, 200))]), "page_key": ["p0", "p1", "p2"]},
            schema=schema,
        ),
        bronze,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )

    _run(lane, bronze, silver)

    out = lance.dataset(silver)
    assert out.schema.names == ["id", "payload", "page_key", "source_rowid", "stage", "thumbnail", "embedding"]
    table = blobs.read_aligned_table(out, columns=["id", "payload", "page_key", "thumbnail", "embedding"]).sort_by("id")
    assert table.column("page_key").to_pylist() == ["p0", "p1", "p2"]
    assert [p is None for p in table.column("payload").to_pylist()] == [False, True, False]
    assert [t is None for t in table.column("thumbnail").to_pylist()] == [False, True, False]
    assert [e is None for e in table.column("embedding").to_pylist()] == [False, True, False]


# --- the declared dataset id and the lineage document describe THIS tier ----------------------------------------------


@pytest.mark.parametrize("wired", [pytest.param(True, id="wired"), pytest.param(False, id="unwired")])
@pytest.mark.parametrize("lane", LANES)
def test_a_tier_declares_its_own_name_and_document_never_its_parents(tmp_path: Path, lane: str, wired: bool) -> None:
    """Schema metadata and a `lineage` cell survive every column operation, so a child would inherit its parent's.

    `maintenance.core.lineage_emit.declared_table_id` files a maintenance run against the declared name, and a gold row
    carrying silver's document claims silver's run. A wired run declares its own; an unwired run declares nothing and
    writes no document, a weaker answer but a true one.
    """
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    parent = '{"run_id": "r-parent"}'
    lance.write_dataset(
        pa.table({"id": pa.array([0, 1], pa.int64()), "lineage": pa.array([parent, parent], pa.json_())}).replace_schema_metadata(
            {LINEAGE_DATASET_ID_KEY: "acme$bronze"}
        ),
        bronze,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )

    _run(lane, bronze, silver, lineage=_doc("r-child") if wired else None, dataset_id="acme$silver" if wired else "")

    out = lance.dataset(silver)
    declared = (out.schema.metadata or {}).get(LINEAGE_DATASET_ID_KEY.encode())
    if wired:
        assert declared == b"acme$silver"
        assert {json.loads(cell)["run_id"] for cell in _by_id(silver, "lineage").values()} == {"r-child"}
    else:
        assert declared is None, f"an unwired run published its parent's name {declared!r}"
        assert "lineage" not in out.schema.names, "an unwired run carried its parent's document"


def test_a_full_run_that_produced_nothing_never_empties_the_tier(tmp_path: Path) -> None:
    """`when_not_matched_by_source_delete` against an empty source matches every row, so it would empty the tier.

    A run that read rows and produced none (a filter that matched nothing, a half-failed distributed write) is refused
    before any commit.
    """
    bronze, silver = str(tmp_path / "bronze.lance"), str(tmp_path / "silver.lance")
    seed_bronze(bronze, {}, rows=3)
    transform_stage(bronze, silver, {}, stage="silver")
    upstream = lance.dataset(bronze)
    produced = stamp_stage(upstream.to_table(with_row_id=True, limit=0), stage="silver", stable_row_ids=True)
    before = lance.dataset(silver).version

    with pytest.raises(tier_write.EmptyFullSyncError, match="empty"):
        tier_write.write_tier(upstream, produced, tier_write.Window(), tier_write.TierTarget(uri=silver, storage_options={}))

    assert lance.dataset(silver).version == before and lance.dataset(silver).count_rows() == 3
