"""The Ray stage job uses the SHARED primitives — it no longer carries copies to pin (B14).

This file used to be a drift-pin: the script inlined `_is_image`, `_derive_thumbnail`,
`_derive_embedding` and the blob-field pair, and these tests asserted each copy stayed
byte-identical to the services'. That is a test comparing two behaviours after the fact, which is
what B14 records as the WRONG fix — it detects divergence rather than preventing it, and only in the
cases someone thought to assert.

The copies are gone. `service_kit.lakehouse.media` and `service_kit.lakehouse.blobs` hold ONE
implementation each, and both drivers import them: the medallion service directly, and this script
too, because the Ray cluster image ships `service-kit` (it already imported `stamp_stage` from it).

So what is left to test is IDENTITY, not agreement: the script must reference the shared function
objects, and a future edit that reintroduces a local copy fails here. The behavioural tests for the
derivers themselves live with the implementation, where they belong.
"""

from __future__ import annotations

import importlib.util
import io
import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pyarrow as pa
import pytest
from lance import blob_array, blob_field

from service_kit.lakehouse import blobs


_JOB_PATH = Path(__file__).parents[2] / "scripts" / "ray_stage_job.py"


def _load_job() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ray_stage_job", _JOB_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


job = _load_job()


@pytest.fixture(scope="module")
def png_bytes() -> bytes:
    """A tiny real PNG (needs Pillow, which the unit venv has via the deriver deps)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (123, 200, 50)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_media_RERUN_keeps_the_row_identity_the_tier_above_resolves_against(tmp_path: Path, png_bytes: bytes) -> None:
    """A second run must RETRACT what the source dropped without re-minting the survivors' `_rowid`.

    The tier above stores `source_rowid`, which is this tier's stable `_rowid`. An overwrite re-mints
    every one of them, so the parent ids the tier above holds stop naming the rows they were written
    for — measured on the live estate as "silver's 8 `source_rowid` values named bronze rows that no
    longer existed, 8 of 8". The tabular head became a full-sync merge for exactly this; the media lane
    could not take that change per-batch, because a per-batch `when_not_matched_by_source_delete` would
    delete the rows earlier batches just wrote.
    """
    import json

    import lance

    src = str(tmp_path / "bronze_media")

    def _write(ids: list[int]) -> None:
        lance.write_dataset(
            pa.table(
                {"id": pa.array(ids, pa.int64()), "payload": blob_array([png_bytes] * len(ids))},
                schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
            ),
            src,
            mode="overwrite",
            data_storage_version="2.2",
            enable_stable_row_ids=True,
        )

    dst = str(tmp_path / "silver_media")
    doc = json.dumps({"schema": "rask.lineage/1", "run_id": "run-one", "job": {"namespace": "n", "name": "j"}, "event_time": "2026-01-01T00:00:00Z"})
    _write([10, 11, 12])
    job._media_transform(src, dst, {}, stage="silver-media", lineage=doc)
    first = {r["id"]: r["_rowid"] for r in lance.dataset(dst).to_table(columns=["id"], with_row_id=True).to_pylist()}
    assert sorted(first) == [10, 11, 12]

    # The source drops 11. A second run must retract it and leave 10 and 12 where they were.
    _write([10, 12])
    doc2 = json.dumps({"schema": "rask.lineage/1", "run_id": "run-two", "job": {"namespace": "n", "name": "j"}, "event_time": "2026-01-02T00:00:00Z"})
    job._media_transform(src, dst, {}, stage="silver-media", lineage=doc2)

    second = {r["id"]: r["_rowid"] for r in lance.dataset(dst).to_table(columns=["id"], with_row_id=True).to_pylist()}
    assert sorted(second) == [10, 12], f"the retraction did not happen: {sorted(second)}"
    assert second[10] == first[10] and second[12] == first[12], (
        f"the survivors' stable row ids were re-minted ({first} -> {second}) — every `source_rowid` the tier above holds now names a row that no longer exists"
    )


def test_stamp_stage_mints_source_rowid_at_the_head_and_carries_it_forward() -> None:
    """The tabular map function's provenance logic (the distributed path can't run in the unit venv).

    Head: a batch carrying the reserved ``_rowid`` metacolumn (no ``source_rowid`` yet) MINTS source_rowid
    from it and never persists ``_rowid``. Carried: a batch that already has ``source_rowid`` keeps it
    UNCHANGED (root provenance, not re-set to the parent's _rowid) and still sheds ``_rowid``. Mirrors
    compute._carry_source_rowid + _stamp_stage.
    """
    head = pa.table({"id": [1, 2], "_rowid": pa.array([40, 41], pa.uint64())})
    stamped = job._stamp_stage(head, "bronze", stable_row_ids=True)
    assert "_rowid" not in stamped.column_names  # reserved metacolumn never persisted
    assert stamped.column("source_rowid").to_pylist() == [40, 41]  # minted from _rowid
    assert stamped.column("stage").to_pylist() == ["bronze", "bronze"]

    carried = pa.table(
        {
            "id": [1, 2],
            "source_rowid": pa.array([40, 41], pa.uint64()),
            "_rowid": pa.array([7, 8], pa.uint64()),
        }
    )
    stamped2 = job._stamp_stage(carried, "silver", stable_row_ids=True)
    assert "_rowid" not in stamped2.column_names
    assert stamped2.column("source_rowid").to_pylist() == [40, 41]  # ROOT id kept, NOT the parent's _rowid


def test_stamp_stage_re_stamps_the_lineage_column_instead_of_inheriting_it() -> None:
    """R26 on the DISTRIBUTED path: the job writes its own provenance document, never the parent's.

    Parity with compute._drop_inherited_lineage + the in-table stamp — the Ray path must not produce a
    governed dataset the in-process path would have stamped, and an inherited cell would label gold rows
    with silver's run. Empty ``lineage`` (the job run by hand) drops the column and adds none.
    """
    doc = '{"run_id": "r-1", "operation": "aggregate_gold"}'
    parent = '{"run_id": "r-0", "operation": "embed_features"}'
    upstream = pa.table(
        {
            "id": [1, 2],
            "source_rowid": pa.array([40, 41], pa.uint64()),
            "lineage": pa.array([parent, parent], pa.json_()),
        }
    )

    stamped = job._stamp_stage(upstream, "gold", doc, stable_row_ids=True)
    assert stamped.column_names.count("lineage") == 1
    assert stamped.schema.field("lineage").type.extension_name == "arrow.json"  # Lance JSONB, not a string
    assert stamped.column("lineage").to_pylist() == [doc, doc]  # THIS run's document, on every row

    bare = job._stamp_stage(upstream, "gold", stable_row_ids=True)
    assert "lineage" not in bare.column_names  # no document handed over → the parent's is still dropped


# ── the media lane streams, rather than holding the whole dataset ────────────────────────────────
#
# Found by the Ray design-patterns audit (2026-08-28) against ray-project's own
# `doc/source/ray-core/patterns/generators.rst`, whose rule is to yield results in chunks rather
# than materialise them all. `_media_transform` did the opposite in the production cascade's MEDIA
# branch: `scanner(blob_handling="all_binary").to_table()` pulled every blob payload into ONE Arrow
# table, `.to_pylist()` made a second full copy as Python bytes, and the thumbnail and embedding
# lists made two more — so peak RSS scaled with the dataset and the media cascade had a hard OOM
# ceiling that nothing announced. It also contradicted the discipline `ratch/core/driver.py`
# documents and enforces two directories away ("heavy blobs never transit Ray Data blocks").
#
# The tabular branch of this same script already streams through lance_ray. Only the media branch
# was all-at-once, and its comments explain why it is driver-side (lance_ray strips blob typing) but
# never why it is unbatched.


def _bronze_media(tmp_path: Path, png_bytes: bytes, rows: int) -> str:
    import lance

    src = str(tmp_path / "bronze_stream")
    table = pa.table(
        {"id": pa.array(list(range(rows)), pa.int64()), "payload": blob_array([png_bytes] * rows)},
        schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
    )
    lance.write_dataset(table, src, data_storage_version="2.2", enable_stable_row_ids=True)
    return src


def test_the_media_transform_never_holds_the_whole_dataset(tmp_path: Path, png_bytes: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the pattern is about: what the driver holds is bounded by the BATCH, not the run.

    Measured at `_media_batch`, the ONE seam every write path takes: it returns the slice that is about
    to be persisted, so a batch of N rows means N rows' payloads, pylists, thumbnails and embeddings
    were all live at once. Spying on `write_dataset` instead measures the write SHAPE — it stopped
    seeing most batches the moment a rerun began merging them rather than appending, while the property
    under test had not changed at all.
    """
    import lance

    src = _bronze_media(tmp_path, png_bytes, rows=7)
    monkeypatch.setattr(job, "MEDIA_BATCH_ROWS", 2)

    widths: list[int] = []
    real_batch = job._media_batch

    def spy(aligned, *args, **kwargs):  # noqa: ANN001, ANN202
        out = real_batch(aligned, *args, **kwargs)
        widths.append(out.num_rows)
        return out

    monkeypatch.setattr(job, "_media_batch", spy)
    dst = str(tmp_path / "silver_stream")
    job._media_transform(src, dst, {}, stage="silver-media")

    assert max(widths) <= 2, f"the driver materialised {max(widths)} rows at once with a batch of 2 — it is not streaming"
    assert sum(widths) == 7, f"rows were lost or duplicated across batches: {widths}"
    assert lance.dataset(dst).count_rows() == 7, "the streamed batches did not all reach the target"


def test_streaming_produces_exactly_what_one_shot_did(tmp_path: Path, png_bytes: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """A smaller peak is worthless if the output changed. Same rows, same order, same artifacts,
    same stable ids — and the derived columns present on EVERY row, not only the first batch's."""
    import lance

    src = _bronze_media(tmp_path, png_bytes, rows=5)
    src_rowids = lance.dataset(src).to_table(with_row_id=True).column("_rowid").to_pylist()

    monkeypatch.setattr(job, "MEDIA_BATCH_ROWS", 2)
    dst = str(tmp_path / "silver_many_batches")
    job._media_transform(src, dst, {}, stage="silver-media", lineage='{"run": "r1"}')

    out = lance.dataset(dst)
    assert out.count_rows() == 5 and out.has_stable_row_ids
    assert blobs.blob_field_names(out.schema) == ["payload"], "blob-v2 typing was demoted by the append path"
    got = out.to_table(columns=["id", "source_rowid", "stage", "thumbnail", "embedding"])
    assert got.column("id").to_pylist() == [0, 1, 2, 3, 4], "the append path reordered rows"
    assert got.column("source_rowid").to_pylist() == src_rowids
    assert set(got.column("stage").to_pylist()) == {"silver-media"}
    assert all(t is not None for t in got.column("thumbnail").to_pylist()), "a later batch skipped derivation"
    assert all(e is not None for e in got.column("embedding").to_pylist())


def test_an_empty_source_still_creates_the_target(tmp_path: Path, png_bytes: bytes) -> None:
    """Zero batches means zero writes, which would have left the target ABSENT — and an absent
    dataset is not the same answer as an empty one to the tier's readers. The unbatched form got
    this for free (one write of a zero-row table); the streamed one has to mean it."""
    import lance

    src = str(tmp_path / "bronze_empty")
    lance.write_dataset(
        pa.table(
            {"id": pa.array([], pa.int64()), "payload": blob_array([])},
            schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
        ),
        src,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )

    dst = str(tmp_path / "silver_empty")
    job._media_transform(src, dst, {}, stage="silver-media")

    out = lance.dataset(dst)
    assert out.count_rows() == 0 and out.has_stable_row_ids
    assert "stage" in out.schema.names and "source_rowid" in out.schema.names


# ── the destination schema and the emitted blocks are ONE construction ───────────────────────────
#
# LIVE BREAK (2026-08-30, deployed rev 87): every TABULAR cascade run reached silver and then FAILED
# at gold. The distributed branch pre-created the destination from a schema it REBUILT — the
# upstream's, minus `stage`/`lineage`, with those two appended back — while `map_batches` emitted
# `stamp_stage`'s output, which re-stamps IN PLACE and therefore keeps the upstream's positions. Over
# the bronze `POST /produce` seeds (`id, payload, stage`) the two disagree from silver onward:
#
#     emitted   ['id', 'payload', 'stage', 'source_rowid', 'lineage']
#     destination ['id', 'payload', 'source_rowid', 'stage', 'lineage']
#
# and `lance_ray` casts every block to the destination's schema by POSITION
# (`lance_ray/pandas.py::pd_to_arrow` -> `df.cast(schema)`), so the append dies with
# `LanceError(Arrow) … Target schema's field names are not matching the table's field names`. Same
# five columns, one transposed pair, the whole cascade down — and it is deterministic, not a race.
#
# The class is the one `stage_stamp`'s module docstring already records: two hand-maintained
# constructions of one schema. That fix unified the two DRIVERS; this one unifies the two sides of a
# single driver's write.


def _bronze_tabular(tmp_path: Path, rows: int = 4) -> str:
    """Bronze exactly as ``medallion.services.compute.seed_bronze`` writes it for ``POST /produce``.

    The column ORDER is the point of this fixture, so it is spelled out rather than derived: `stage`
    sits third, ahead of the `source_rowid` the first stage mints, and that is what makes the two
    constructions disagree from silver onward.
    """
    import lance

    src = str(tmp_path / "bronze_tabular")
    lance.write_dataset(
        pa.table(
            {
                "id": pa.array(list(range(rows)), pa.int64()),
                "payload": pa.array([f"event-{i}" for i in range(rows)]),
                "stage": pa.array(["bronze"] * rows, pa.string()),
            }
        ),
        src,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    return src


def test_the_media_batch_keeps_the_column_order_the_media_lane_already_writes(tmp_path: Path, png_bytes: bytes) -> None:
    """The media branch MUST NOT move: its lane writes with pylance, whose overwrite takes the
    table's schema as the dataset's, so a reordering here silently rewrites a governed tier's shape.

    Pinned as the literal order because that is the property — carried columns in upstream order,
    then root provenance, this stage's stamp, this run's document, and the derived artifacts last.
    """
    import lance

    src = str(tmp_path / "bronze_order")
    lance.write_dataset(
        pa.table(
            {"id": pa.array([1], pa.int64()), "payload": blob_array([png_bytes]), "source_uri": pa.array(["s3://b/k"], pa.string())},
            schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("source_uri", pa.string())]),
        ),
        src,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )

    dst = str(tmp_path / "silver_order")
    job._media_transform(src, dst, {}, stage="silver", lineage='{"run_id": "r-1"}')

    assert lance.dataset(dst).schema.names == ["id", "payload", "source_uri", "source_rowid", "stage", "lineage", "thumbnail", "embedding"]


@pytest.fixture
def isolated_ray(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A PRIVATE in-process Ray for the distributed branch — never the estate's live cluster.

    `ray.init()` with no address ADOPTS whichever cluster it discovers; measured on this host it
    found four live instances, the k3s KubeRay head among them, so an unpinned init would schedule a
    unit test's write fragments onto the deployed estate. `address="local"` forces a fresh one. The
    uv runtime-env hook is off because it packages the working directory into every worker, which
    this test has no use for and which fails outright when pytest's cwd is not the project root.
    """
    ray = pytest.importorskip("ray")
    from ray._private import ray_constants

    monkeypatch.setattr(ray_constants, "RAY_ENABLE_UV_RUN_RUNTIME_ENV", False, raising=False)
    ray.init(address="local", num_cpus=2, include_dashboard=False, log_to_driver=False, configure_logging=False)
    try:
        yield
    finally:
        ray.shutdown()


@pytest.mark.slow
def test_the_tabular_cascade_reaches_gold_through_the_real_distributed_write(tmp_path: Path, isolated_ray: None) -> None:
    """The reported break, reproduced and then closed with the real lance_ray write on a real Ray.

    `slow` because it starts a Ray cluster — no other test in this estate does, and the default suite
    is measured in seconds.
    """
    import lance

    bronze = _bronze_tabular(tmp_path)
    silver, gold = str(tmp_path / "silver_e2e"), str(tmp_path / "gold_e2e")

    job._run_stage(bronze, silver, "silver", {}, lineage='{"run_id": "r-silver"}')
    job._run_stage(silver, gold, "gold", {}, lineage='{"run_id": "r-gold"}')

    upstream, out = lance.dataset(silver), lance.dataset(gold)
    assert out.schema.names == upstream.schema.names  # the append cast nothing into a new shape
    assert out.count_rows() == upstream.count_rows() and out.has_stable_row_ids
    assert out.to_table(columns=["stage"]).column("stage").to_pylist() == ["gold"] * 4  # re-stamped, not inherited
    assert sorted(out.to_table(columns=["source_rowid"]).column("source_rowid").to_pylist()) == sorted(
        upstream.to_table(columns=["source_rowid"]).column("source_rowid").to_pylist()
    )  # root provenance carried, not re-minted off silver


def test_a_second_media_hop_neither_duplicates_the_artifacts_nor_keeps_reshaping(tmp_path: Path, png_bytes: bytes) -> None:
    """The media lane must survive more than ONE hop, and this branch could not.

    A tier that already carries `thumbnail`/`embedding` carries them forward through the scan, and the
    derivation appended a SECOND pair on top: `LanceError(Schema): Duplicate field name "thumbnail"`,
    so bronze→silver worked and silver→gold died. The in-process driver's `derive_artifacts` has
    exactly this guard ("skips when the upstream already carries the artifact columns"); the Ray copy
    did not, and the two column names were spelled twice, once per driver.

    Shape STABILITY is asserted from the second hop on, not against the first: the head MINTS
    `source_rowid` and appends it, so bronze's shape and silver's cannot be equal — bronze has no
    provenance columns at all. What must hold is that a stage stops reshaping once they exist.
    """
    import lance

    src = str(tmp_path / "bronze_hops")
    lance.write_dataset(
        pa.table(
            {"id": pa.array([1, 2], pa.int64()), "payload": blob_array([png_bytes, png_bytes])},
            schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
        ),
        src,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    silver, gold, platinum = str(tmp_path / "silver_hops"), str(tmp_path / "gold_hops"), str(tmp_path / "platinum_hops")

    job._media_transform(src, silver, {}, stage="silver", lineage='{"run_id": "r-silver"}')
    job._media_transform(silver, gold, {}, stage="gold", lineage='{"run_id": "r-gold"}')
    job._media_transform(gold, platinum, {}, stage="platinum", lineage='{"run_id": "r-platinum"}')

    up, out, again = lance.dataset(silver), lance.dataset(gold), lance.dataset(platinum)
    assert sorted(out.schema.names) == sorted(up.schema.names)  # same columns, derived exactly once
    assert len(out.schema.names) == len(set(out.schema.names))  # and no second `thumbnail`
    assert again.schema.equals(out.schema), "the lane is still reshaping the tier hop after hop"
    assert blobs.blob_field_names(out.schema) == ["payload"]  # blob typing survives the hop
    assert out.to_table(columns=["stage"]).column("stage").to_pylist() == ["gold", "gold"]  # re-stamped, not inherited
    # Lance normalises the JSONB text, so the document is compared parsed rather than byte-wise.
    assert [json.loads(cell) for cell in out.to_table(columns=["lineage"]).column("lineage").to_pylist()] == [{"run_id": "r-gold"}] * 2
    assert (
        out.to_table(columns=["source_rowid"]).column("source_rowid").to_pylist() == up.to_table(columns=["source_rowid"]).column("source_rowid").to_pylist()
    )  # root provenance carried, not re-minted off silver
    assert out.to_table(columns=["thumbnail"]).column("thumbnail").to_pylist() == up.to_table(columns=["thumbnail"]).column("thumbnail").to_pylist()


def test_the_distributed_branch_creates_its_destination_from_the_SAME_construction_it_emits(tmp_path: Path) -> None:
    """The gold-tier invariant, driven through `_run_stage`'s real distributed branch — and FAST.

    This exists because its slow sibling could not do the job. That one compares `_target_schema(...)`
    against `_stamp_stage(...)`, but `_target_schema` is DEFINED as that stamp, so both sides are one
    function and the assertion holds no matter what the call site does. Proven by mutation: reinstating
    the pre-fix inline construction inside `_run_stage` left the whole non-slow file green. Since
    `make test` and CI both run `-m "not slow"`, the only test that could catch a recurrence of the
    break that killed every tabular cascade at gold never ran in the gate.

    So this one asserts on the CALL SITE. `lance_ray` is stubbed to capture two things the production
    path produces: the schema the destination is created with, and the block the REAL `map_batches`
    lambda emits for a real upstream batch. `lance_ray` appends by casting each block to the
    destination positionally, so those two orders must be one construction — agreeing by luck is the
    failure mode, and comparing names in order is what detects it.
    """
    import lance

    job = _load_job()

    # A RE-STAMP shape — silver already carrying `stage` and `lineage`, which is what silver->gold is.
    # That is the only case where the two constructions diverge: the pre-fix code REMOVED those two and
    # re-APPENDED them at the end, while `stamp_stage` replaces them IN PLACE. Against a bronze-shaped
    # upstream (no stage, no lineage) both agree, so a fixture built from bronze cannot catch the bug —
    # my first attempt at this test used one and passed against the reinstated break.
    src = str(tmp_path / "silver.lance")
    lance.write_dataset(
        pa.table(
            {
                "id": pa.array(["a", "b"], pa.string()),
                "payload": pa.array([b"x", b"y"], pa.large_binary()),
                "stage": pa.array(["silver", "silver"], pa.string()),
                "source_rowid": pa.array([0, 1], pa.uint64()),
                "lineage": pa.array([b'{"run":"r"}', b'{"run":"r"}'], pa.json_()),
            }
        ),
        src,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    dst = str(tmp_path / "gold.lance")

    created: dict[str, pa.Schema] = {}
    emitted: dict[str, pa.Schema] = {}

    class _Blocks:
        """Stands in for a Ray Dataset: runs the production lambda over one real batch."""

        def __init__(self, table: pa.Table) -> None:
            self.table = table

        def map_batches(self, fn, **_kw):  # noqa: ANN001, ANN003, ANN202 — the stub mirrors Ray's shape
            self.table = fn(self.table)
            emitted["schema"] = self.table.schema
            return self

    class _LanceRay:
        """Enough of `lance_ray` to drive the real branch: read, map, append what the lambda produced.

        The append is REAL, so `_run_stage`'s own post-write row-count and stable-row-id checks still
        run — a stub that wrote nothing would trip them and mask whatever this test is asserting.
        """

        def read_lance(self, uri: str, **_kw):  # noqa: ANN003, ANN202
            return _Blocks(lance.dataset(uri).to_table())

        def write_lance(self, blocks, uri: str, **_kw) -> None:  # noqa: ANN001, ANN003
            real_write(blocks.table, uri, mode="append", data_storage_version="2.2")

    real_write = lance.write_dataset

    def _capture_write(data, uri, **kw):  # noqa: ANN001, ANN003, ANN202
        # THE DATASET `lance_ray` APPENDS INTO, which is the STAGING set: the distributed output lands
        # there and one merge converges it into the destination (LH-007, `_land_staged`). The property
        # under test is unchanged and belongs to whichever dataset takes the appends — lance_ray casts
        # every block to it positionally, so its schema and the emitted block's must agree on ORDER.
        if str(uri).startswith(dst) and kw.get("mode") == "overwrite":
            created["schema"] = data.schema
        return real_write(data, uri, **kw)

    import sys

    # `sys.modules` is typed `dict[str, ModuleType]`, and the stub is deliberately a plain object that
    # answers the two attributes the job imports. Cast rather than suppress: a mypy-style suppression
    # is the wrong tool's syntax here, and ty does not honour it.
    sys.modules["lance_ray"] = cast("ModuleType", _LanceRay())
    try:
        job.lance.write_dataset = _capture_write  # the job holds its own `lance` reference
        job._run_stage(from_uri=src, to_uri=dst, stage="gold", lineage='{"run":"r2"}', so={})
    finally:
        job.lance.write_dataset = real_write
        sys.modules.pop("lance_ray", None)

    assert created.get("schema") is not None, "the distributed branch never created its destination"
    assert emitted.get("schema") is not None, "the production map_batches lambda never ran"
    assert created["schema"].names == emitted["schema"].names, (
        "the destination schema and the block the transform emits disagree on column ORDER. "
        "`lance_ray` casts every appended block to the destination positionally, so this is the exact "
        f"shape that killed the gold tier: destination={created['schema'].names} "
        f"emitted={emitted['schema'].names}"
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# D1 — THE DELTA BOUNDARY, AND THE CARDINALITY CONTRACT THAT COMES WITH IT.
#
# The submitter has always exported `BASE_VERSION`, and the publication event has always carried
# the `{from_version, to_version}` range it comes from. The generic stage job never read it: a grep of
# this script for BASE_VERSION matched NOTHING, so every run — including a backfill that added two
# rows to a million-row bronze — reopened the whole upstream and rewrote the destination with
# `mode="overwrite"`. Cost scaled with the TIER, not with the delta, and the range the publication
# event exists to carry was decorative on the only lane that ships by default.
#
# `ray_dummy_job.py` — baked into the SAME cluster image — has done this correctly all along:
# `_row_created_at_version > base` for the read, `merge_insert` for the write. These tests hold the
# generic job to the standard its sibling already meets.
#
# THE ROW-COUNT ASSERTION HAD TO GO WITH IT, and that is not a loosening. `out.count_rows() ==
# upstream.count_rows()` is unstatable once a run processes a delta — the destination legitimately
# holds rows this run never looked at. What the assertion was really protecting is PROVENANCE: that
# no row arrives without a parent. That property is checkable on a delta run, on a fan-out run, and
# on a full run alike, and it is strictly stronger than counting.


def _versions_of(uri: str) -> int:
    import lance

    return lance.dataset(uri).version


def test_a_delta_run_reprocesses_only_the_rows_added_since_BASE_VERSION(tmp_path: Path) -> None:
    """The backfill property: two rows added to an existing tier must move two rows, not the tier.

    Observed through the STAGE STAMP rather than a row count, because a count cannot tell the two
    apart — a full rescan and a correct delta both leave six rows in silver. Re-running with a
    different stage label means only the rows this run actually read carry the new one.

    Note the destination must already exist: a delta against a destination that cannot merge degrades
    to a full run on purpose (see `_mergeable`), because writing a delta into a table `_reset_if_legacy`
    just wiped is silent data loss. That degradation is what the first version of this test hit.
    """
    import lance

    bronze = _bronze_tabular(tmp_path, rows=4)
    silver = str(tmp_path / "silver_delta")
    job._run_stage(bronze, silver, "first-pass", {}, lineage='{"run_id": "r-full"}')
    base = _versions_of(bronze)

    lance.write_dataset(
        pa.table({"id": pa.array([100, 101], pa.int64()), "payload": pa.array(["late-a", "late-b"]), "stage": pa.array(["bronze"] * 2, pa.string())}),
        bronze,
        mode="append",
        data_storage_version="2.2",
    )
    job._run_stage(bronze, silver, "backfill-pass", {}, lineage='{"run_id": "r-delta"}', base_version=base)

    out = lance.dataset(silver).to_table(columns=["id", "stage"]).to_pylist()
    by_id = {row["id"]: row["stage"] for row in out}
    assert len(by_id) == 6, f"the merge did not converge: {sorted(by_id)}"
    assert by_id[100] == "backfill-pass" and by_id[101] == "backfill-pass", "the new rows were not processed"
    assert [by_id[i] for i in range(4)] == ["first-pass"] * 4, f"the delta run reprocessed rows it should never have read: {by_id}"


def test_a_redelivered_delta_CONVERGES_instead_of_duplicating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dapr redelivers. A second run over the same delta must leave the destination unchanged.

    `merge_insert` on the key, never `append`: an append would double every row on the redelivery that
    at-least-once delivery guarantees will eventually happen, and nothing downstream would notice.

    The redelivery here also RACES a concurrent marked run: that run's merge commits between the redelivery's plan
    and its commit, which preempts it (a retryable conflict on real pylance). Both must land, and the redelivery's
    marker must end on the destination's last commit, or the planner reads a run that wrote as one that did not.
    """
    import lance

    from service_kit.lakehouse.commit_marker import CommitMarker, marked_version

    bronze = _bronze_tabular(tmp_path, rows=3)
    silver = str(tmp_path / "silver_converge")

    job._run_stage(bronze, silver, "silver", {}, lineage='{"run_id": "r-1"}')
    first = lance.dataset(silver).count_rows()
    before = lance.dataset(silver).version

    real_stamped = job.stamped
    raced: list[str] = []

    def a_concurrent_run_commits_first(transaction: lance.Transaction, marker: CommitMarker) -> lance.Transaction:
        if not raced:
            raced.append(marker.action_id)
            rows = job._stamp_stage(lance.dataset(bronze).to_table(with_row_id=True), "silver", '{"run_id": "r-1"}', "", stable_row_ids=True)
            job._converge(silver, rows, {}, CommitMarker(action_id="concurrent-run"), full_sync=True)
        return real_stamped(transaction, marker)

    monkeypatch.setattr(job, "stamped", a_concurrent_run_commits_first)
    job._run_stage(bronze, silver, "silver", {}, lineage='{"run_id": "r-1"}', marker=CommitMarker(action_id="redelivery", run_id="r-1"))

    assert lance.dataset(silver).count_rows() == first == 3, "the redelivery duplicated rows"
    latest = lance.dataset(silver).version
    assert raced == ["redelivery"], "the concurrent run never raced the redelivery's commit"
    assert marked_version(silver, {}, action_id="concurrent-run", above=before) == before + 1, "the concurrent run's merge did not land"
    assert marked_version(silver, {}, action_id="redelivery", above=before) == latest == before + 2, (
        "the redelivery that lost the commit race did not re-plan and land on the destination's last commit"
    )


def test_a_FANOUT_lane_is_allowed_and_its_provenance_is_complete() -> None:
    """One input row becoming many is a legitimate governed shape — a video into frames, a recording
    into speaker turns. It was refused outright by a row-count equality a fan-out can never satisfy.

    What replaces it is the property the count was standing in for: EVERY output row names a parent.
    That holds for 1:1, for a fan-out and for a filter alike, and it is strictly stronger than counting
    — a transform that swapped two rows for two different ones passed the old check and fails this one.

    Driven as a PURE function rather than through a job parameter: the generic job is stamp-only by
    design, so a `transform=` hook added to make this testable would be a production seam existing for
    a test. The contract is the thing under test, so the contract is what the test calls.

    `parentless` rather than the id list, because the call site must evaluate this on a tier that may
    hold millions of rows: a null-count pushdown is one cheap scan, materialising every parent id is
    not. The mapping itself (which parent each child names) is asserted in the integration test below.
    """
    job._assert_stage_contract(rows_in=3, rows_out=6, cardinality="1:N", parentless=0)


def test_a_fanout_row_with_NO_parent_is_refused() -> None:
    """The half that makes the relaxation safe. Dropping the count check without this would let a
    fan-out lane write rows that descend from nothing, which is exactly the ungoverned shape the
    1:1 rule was over-enforcing against."""
    with pytest.raises(SystemExit, match="parent"):
        job._assert_stage_contract(rows_in=3, rows_out=4, cardinality="1:N", parentless=1)


def test_a_1to1_lane_still_REFUSES_a_transform_that_changes_the_row_count() -> None:
    """The original guard, kept where it belongs — scoped to the lanes that actually claim 1:1
    rather than forbidding the shape estate-wide."""
    with pytest.raises(SystemExit, match="row count"):
        job._assert_stage_contract(rows_in=3, rows_out=6, cardinality="1:1", parentless=0)


def test_a_1to1_lane_that_holds_its_count_passes() -> None:
    """The ordinary case: nothing about the default lane changes."""
    job._assert_stage_contract(rows_in=3, rows_out=3, cardinality="1:1", parentless=0)


def test_an_unknown_cardinality_is_refused_rather_than_defaulted() -> None:
    """A typo in a declared lane must not silently buy the loosest contract — the failure mode of
    every string-typed policy that falls back to a default."""
    with pytest.raises(SystemExit, match="cardinality"):
        job._assert_stage_contract(rows_in=3, rows_out=3, cardinality="one-to-many", parentless=0)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#
# …AND A LANE MUST BE ABLE TO DECLARE IT. The contract above is unreachable if nothing sets
# STAGE_CARDINALITY: the job would support a fan-out that no project can ask for, which is the
# half-built shape this estate keeps finding in its own audits. `TransformSpec` is where a project
# already declares its task and params through the catalog's admin-gated door, so it is where
# the cardinality belongs too.


def test_a_declared_cardinality_the_job_cannot_honour_is_refused_at_DECLARATION_time() -> None:
    """Refused at the door, not at 3am on the cluster. The job refuses an unknown cardinality too,
    but by then a Ray job has been submitted and the operator sees a stage FAIL instead of a 422."""
    import pydantic

    from service_kit.lakehouse.transform_specs import TransformSpec

    with pytest.raises(pydantic.ValidationError):
        TransformSpec(name="x", project="acme", from_id="a", to_id="b", task="stage-transform", cardinality="one-to-many")


def test_the_submit_path_FORWARDS_the_declared_cardinality_to_the_job(tmp_path: Path) -> None:
    """The link that makes the declaration real. Without it a project can declare a fan-out lane, the
    catalog stores it, and the job runs under the 1:1 default that refuses the very shape declared.

    Driven through the stage lane's order builder over a REAL declaration on `tmp_path`: the value the job
    reads as `RASK_CARDINALITY` is the order's stamp (`to_env()`, pinned by
    `tests/unit/test_the_submitter_and_the_job_agree_on_the_wire.py`), so the stamp is what is asserted.
    """
    import asyncio

    from medallion.core.config import MedallionSettings
    from medallion.services import stage_submit
    from service_kit.lakehouse import task_registry, transform_specs
    from service_kit.lakehouse.stage_stamp import ONE_TO_MANY
    from service_kit.lakehouse.task_registry import TaskRegistration
    from service_kit.lakehouse.transform_specs import TransformSpec

    task_registry.put_task(str(tmp_path), {}, TaskRegistration(task="frames", engine="ray", command="python /home/ray/jobs/ray_stage_job.py"))
    transform_specs.put_spec(
        str(tmp_path),
        {},
        TransformSpec(name="video", project="acme", from_id="acme-bronze$videos", to_id="acme-silver$frames", task="frames", cardinality=ONE_TO_MANY),
    )
    settings = MedallionSettings.model_validate(
        {"compute_enabled": True, "ray_enabled": True, "control_root": str(tmp_path), "transform": "video", "to_namespace": "silver"}
    )

    order, _registration = asyncio.run(
        stage_submit.build_stage_order(settings, from_uri="s3://acme/bronze", to_uri="s3://acme/silver", stage="silver", token="t", project="acme")
    )

    assert order.stamp.cardinality == ONE_TO_MANY, (
        "the declared fan-out did not reach the order's stamp, so the lane runs under the 1:1 default that refuses the very shape it declared"
    )
    assert order.to_env()["RASK_CARDINALITY"] == ONE_TO_MANY


def test_a_derived_TIER_declares_ITS_OWN_canonical_name_not_its_parents(tmp_path: Path) -> None:
    """`lineage.dataset_id` is schema METADATA, so it survives every column operation the stamp performs.

    The order has always carried `RASK_DEST_TABLE` and this job never read it, so each derived tier
    inherited its UPSTREAM's canonical name — and `maintenance.core.lineage_emit.declared_table_id`
    reads exactly that key to decide which dataset a maintenance run is filed against. Silver's
    compactions, and silver's per-dataset FAIL events (the estate's only per-dataset maintenance
    failure surface), were being written against bronze's node.

    Driven through `_run_stage` rather than the stamp alone, because the defect was in the THREAD, not
    in the stamp: every piece existed and nothing connected them.
    """
    import lance

    from service_kit.lakehouse.stage_stamp import LINEAGE_DATASET_ID_KEY

    bronze_uri = _bronze_tabular(tmp_path)
    bronze = lance.dataset(bronze_uri)
    lance.write_dataset(
        bronze.to_table().replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze"}),
        bronze_uri,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    assert (lance.dataset(bronze_uri).schema.metadata or {})[LINEAGE_DATASET_ID_KEY.encode()] == b"acme$bronze"

    silver_uri = str(tmp_path / "silver_declared")
    job._run_stage(bronze_uri, silver_uri, "silver", {}, lineage='{"run_id": "r-silver"}', dataset_id="acme$silver")

    declared = (lance.dataset(silver_uri).schema.metadata or {}).get(LINEAGE_DATASET_ID_KEY.encode())
    assert declared == b"acme$silver", f"silver declares {declared!r}, so its maintenance provenance is filed against the wrong dataset"


def test_an_UNWIRED_run_declares_NOTHING_rather_than_its_parents_name(tmp_path: Path) -> None:
    """Absent is not blank and neither is inherited. A run with no destination name on the wire must
    publish no name at all: `declared_table_id` then returns None and the sweep falls back to the URI
    derivation, which is a weaker answer but a TRUE one — where an inherited name is a confident lie."""
    import lance

    from service_kit.lakehouse.stage_stamp import LINEAGE_DATASET_ID_KEY

    bronze_uri = _bronze_tabular(tmp_path)
    bronze = lance.dataset(bronze_uri)
    lance.write_dataset(
        bronze.to_table().replace_schema_metadata({LINEAGE_DATASET_ID_KEY: "acme$bronze"}),
        bronze_uri,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )

    silver_uri = str(tmp_path / "silver_unwired")
    job._run_stage(bronze_uri, silver_uri, "silver", {}, lineage='{"run_id": "r-silver"}')

    assert LINEAGE_DATASET_ID_KEY.encode() not in (lance.dataset(silver_uri).schema.metadata or {})


# --------------------------------------------------------------------------- #
# LH-007: the distributed landing preserves tier row identity
# --------------------------------------------------------------------------- #


def _tier_table(ids: list[int]) -> pa.Table:
    return pa.table({"id": [str(i) for i in ids], "payload": [f"v{i}" for i in ids]})


def _seed_tier(uri: str, ids: list[int]) -> None:
    import lance

    lance.write_dataset(_tier_table(ids), uri, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)


def _rowids(uri: str) -> dict[str, int]:
    import lance

    table = lance.dataset(uri).to_table(columns=["id"], with_row_id=True)
    return dict(zip(table.column("id").to_pylist(), table.column("_rowid").to_pylist(), strict=True))


def test_landing_a_staged_dataset_PRESERVES_the_tier_rowids(tmp_path: Path) -> None:
    """CONTRACT (LH-007): a re-derivation keeps `_rowid` for every row that survives it.

    The distributed branch wrote an EMPTY table with `mode="overwrite"` and then distributed-appended
    the transform's output, so every run re-minted `_rowid` for the whole tier. The tier ABOVE resolves
    its `source_rowid` against exactly those values, so the cascade's provenance chain was destroyed on
    a schedule — measured on this estate as 8 of 8 silver references naming bronze rows that no longer
    existed.

    An append cannot be made to preserve identity: `lance_docs/file_format.md:3998` — "Writer assigns
    row IDs sequentially starting from `next_row_id` for new rows" — while only an update remaps one
    (`:4025`, "The new physical row is assigned the same `_rowid = R`"). So the landing has to be a
    merge, and the distributed output has to reach a staging dataset first because `lance_ray` offers
    no distributed merge and `enable_stable_row_ids` is create-time-only.
    """
    job = _load_job()
    to_uri = str(tmp_path / "silver")
    scratch = str(tmp_path / "scratch")
    _seed_tier(to_uri, [1, 2, 3])
    before = _rowids(to_uri)
    # The staged output of a re-derivation: same keys, new payloads, plus one new row.
    _seed_tier(scratch, [1, 2, 3, 4])

    job._land_staged(to_uri, scratch, {})

    after = _rowids(to_uri)
    assert {k: after[k] for k in before} == before, f"a surviving row changed identity: {before} -> {after}"
    assert "4" in after, "the new row never landed"


def test_landing_DROPS_a_row_the_run_no_longer_produces(tmp_path: Path) -> None:
    """Full-sync semantics survive the change: the run's output IS the whole tier.

    `overwrite` removed a row the run stopped producing, and the merge has to keep doing that or the
    tier silently accumulates rows no upstream backs. That is what `when_not_matched_by_source_delete`
    is for — and it is also the clause that makes an empty source catastrophic, which the next test
    pins.
    """
    job = _load_job()
    to_uri = str(tmp_path / "silver")
    scratch = str(tmp_path / "scratch")
    _seed_tier(to_uri, [1, 2, 3])
    _seed_tier(scratch, [1, 3])

    job._land_staged(to_uri, scratch, {})

    assert sorted(_rowids(to_uri)) == ["1", "3"], "a row the run no longer produces was not retracted"


def test_an_EMPTY_staged_dataset_never_empties_the_tier(tmp_path: Path) -> None:
    """THE CATASTROPHIC CASE, and the reason this landed as a named function rather than inline.

    `when_not_matched_by_source_delete()` against a source with zero rows matches every row in the
    destination and deletes the entire tier. A Ray stage that produced nothing — an upstream read that
    came back empty, a transform that filtered everything, a partial failure — must not be read as
    "the tier is now empty".

    The estate already refuses exactly this one lane over: the media retraction runs only
    `if written and run`, because "absent provenance must fail SAFE, not destructively". The sibling
    merge in `compute.py` carries NO such guard, so copying that call site verbatim is how the hazard
    would arrive here.
    """
    job = _load_job()
    to_uri = str(tmp_path / "silver")
    scratch = str(tmp_path / "scratch")
    _seed_tier(to_uri, [1, 2, 3])
    _seed_tier(scratch, [])

    with pytest.raises(Exception) as caught:  # noqa: B017 — the TYPE is asserted below via the message
        job._land_staged(to_uri, scratch, {})

    assert sorted(_rowids(to_uri)) == ["1", "2", "3"], "an empty staged dataset emptied the tier"
    assert "empt" in str(caught.value).lower(), f"the refusal does not say why: {caught.value}"


def test_an_ABSENT_staged_dataset_is_not_read_as_an_empty_one(tmp_path: Path) -> None:
    """A `write_lance` that returned without committing leaves NOTHING at the staging path.

    Reading that as an empty source is the previous test's annihilation by another route, so it is
    refused on its own and says which of the two happened — an operator debugging a lost stage needs to
    know whether the transform produced nothing or never wrote at all.
    """
    job = _load_job()
    to_uri = str(tmp_path / "silver")
    _seed_tier(to_uri, [1, 2, 3])

    with pytest.raises(Exception) as caught:  # noqa: B017
        job._land_staged(to_uri, str(tmp_path / "never-written"), {})

    assert sorted(_rowids(to_uri)) == ["1", "2", "3"], "an absent staged dataset emptied the tier"
    assert "staged" in str(caught.value).lower() or "absent" in str(caught.value).lower(), caught.value


def test_a_staging_set_that_cannot_be_dropped_SAYS_SO(capsys) -> None:
    """CONTRACT: cleanup never raises, and never fails silently either.

    `_drop_staged` runs after the landing has already committed, so a propagating error would turn a
    completed, correctly-landed stage into a reported failure and invite a re-run of finished work. It
    therefore swallows — and swallowing without a word leaves an orphaned Lance dataset under the
    destination that nothing reports and nobody looks for, which is the silent-failure half of the same
    anti-pattern.
    """
    job = _load_job()

    job._drop_staged("s3://nowhere/never-written/_staging/run-1", {"endpoint": "http://127.0.0.1:1"})

    printed = capsys.readouterr().out
    assert "staging" in printed.lower(), f"an undroppable staging set left no trace: {printed!r}"
    assert "_staging/run-1" in printed, printed


# THE DELTA BOUNDARY'S SECOND HALF (LH-008). The backfill lane asked `_row_created_at_version > N`,
# which is the change feed's INSERTED predicate (`lance_docs/file_format.md:4277-4285`) — so a row the
# upstream corrected in place after the boundary was never re-derived and the tier below kept a stale
# payload with nothing reporting it. The three cases below are the whole boundary: changed-by-insert,
# changed-by-update, and unchanged. Bugs cluster at a predicate, so all three are pinned, not just the
# one that fired.


def _delta_run(bronze: str, silver: str, tmp_path: Path) -> dict[int, str]:
    """Seed the tier with a full run, then hand back a reader for the id → payload the tier holds."""
    import lance

    landed = lance.dataset(silver).to_table(columns=["id", "payload"]).to_pydict()
    return dict(zip(landed["id"], landed["payload"], strict=True))


def test_an_in_place_UPDATE_since_the_boundary_reaches_the_tier_below(tmp_path: Path) -> None:
    """A corrected upstream row must re-derive; the insert-only predicate silently kept the old one."""
    import lance

    bronze = _bronze_tabular(tmp_path, rows=3)
    silver = str(tmp_path / "silver_updated")
    job._run_stage(bronze, silver, "silver", {}, lineage='{"run_id": "r-full"}')
    assert _delta_run(bronze, silver, tmp_path)[1] == "event-1"

    boundary = lance.dataset(bronze).version
    lance.dataset(bronze).update({"payload": "'corrected'"}, where="id = 1")

    job._run_stage(bronze, silver, "silver", {}, lineage='{"run_id": "r-delta"}', base_version=boundary)

    assert _delta_run(bronze, silver, tmp_path)[1] == "corrected", "an in-place update never reached the tier below"


# RETRACTION (LH-132). The delta lane and the full lane disagreed about a deleted upstream row: a full
# run drops it (`when_not_matched_by_source_delete`), a delta run left it standing and logged
# `delta_empty=1` — "nothing changed" about a retraction. Whether a delete propagated therefore depended
# on which lane the scheduler picked.


def test_a_row_DELETED_upstream_is_retracted_from_the_tier_below(tmp_path: Path) -> None:
    """The lane's own measurement: a deletion-only change IS an empty delta, so this runs ahead of it."""
    import lance

    bronze = _bronze_tabular(tmp_path, rows=3)
    silver = str(tmp_path / "silver_retract")
    job._run_stage(bronze, silver, "silver", {}, lineage='{"run_id": "r-full"}')

    boundary = lance.dataset(bronze).version
    lance.dataset(bronze).delete("id = 1")

    job._run_stage(bronze, silver, "silver", {}, lineage='{"run_id": "r-delta"}', base_version=boundary)

    held = lance.dataset(silver).to_table(columns=["id"]).to_pydict()["id"]
    assert sorted(held) == [0, 2], "the delta lane kept a row the upstream had retracted"


def test_the_two_lanes_AGREE_about_a_deleted_row(tmp_path: Path) -> None:
    """The defect was a divergence, so the pin is the agreement rather than either lane's answer."""
    import lance

    bronze = _bronze_tabular(tmp_path, rows=4)
    delta_dst, full_dst = str(tmp_path / "silver_by_delta"), str(tmp_path / "silver_by_full")
    for dst in (delta_dst, full_dst):
        job._run_stage(bronze, dst, "silver", {}, lineage='{"run_id": "r-full"}')

    boundary = lance.dataset(bronze).version
    lance.dataset(bronze).delete("id = 2")

    job._run_stage(bronze, delta_dst, "silver", {}, lineage='{"run_id": "r-delta"}', base_version=boundary)
    job._run_stage(bronze, full_dst, "silver", {}, lineage='{"run_id": "r-rescan"}')

    by_delta = sorted(lance.dataset(delta_dst).to_table(columns=["id"]).to_pydict()["id"])
    by_full = sorted(lance.dataset(full_dst).to_table(columns=["id"]).to_pydict()["id"])
    assert by_delta == by_full, "which lane ran still decides whether a delete propagates"


@pytest.mark.parametrize("hop", ["head", "deeper"])
def test_a_retraction_reads_only_what_lance_recorded_as_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hop: str) -> None:
    """A delete and a compaction in one window: the tier loses exactly the deleted row, and no key column is read whole.

    The deleted set comes from Lance's own record (`delta(...).get_deleted_row_ids()`), so a compaction,
    which deletes no row and keeps every stable id, retracts nothing (measured on pylance 12.0.0: a
    Rewrite window answers `[]`). At the head the deleted `_rowid`s are the keys; deeper in they are
    mapped to `source_rowid` through the upstream at the boundary. Every scan the run issues is recorded
    off the real scanner: a retraction sized by the tier shows up as an unfiltered read of `_rowid` or
    `source_rowid`.
    """
    import lance

    upstream = _bronze_tabular(tmp_path, rows=4)
    if hop == "deeper":
        silver = str(tmp_path / "silver_upstream")
        job._run_stage(upstream, silver, "silver", {}, lineage='{"run_id": "r-silver"}')
        upstream = silver
    tier = str(tmp_path / "tier_compacted")
    if hop == "head":
        job._run_stage(upstream, tier, "tier", {}, lineage='{"run_id": "r-full"}')
    else:
        # A full run off a tier that already carries `source_rowid` distributes on Ray; the tier is the
        # same rows written directly, which is all the delta run below needs of it.
        lance.write_dataset(lance.dataset(upstream).to_table(), tier, data_storage_version="2.2", enable_stable_row_ids=True)
    boundary = lance.dataset(upstream).version
    lance.dataset(upstream).delete("id = 1")
    lance.dataset(upstream).optimize.compact_files(target_rows_per_fragment=1024)

    scans: list[tuple[tuple[str, ...], str | None]] = []
    real_scanner = lance.LanceDataset.scanner

    def recording_scanner(self: lance.LanceDataset, *args: Any, **kwargs: Any) -> lance.LanceScanner:
        columns = kwargs.get("columns")
        scans.append((tuple(columns) if isinstance(columns, list) else (), cast("str | None", kwargs.get("filter"))))
        return real_scanner(self, *args, **kwargs)

    monkeypatch.setattr(lance.LanceDataset, "scanner", recording_scanner)
    job._run_stage(upstream, tier, "tier", {}, lineage='{"run_id": "r-delta"}', base_version=boundary)

    held = sorted(lance.dataset(tier).to_table(columns=["id"]).to_pydict()["id"])
    assert held == [0, 2, 3], f"the tier did not lose exactly the deleted row: {held}"
    whole_key_reads = [scan for scan in scans if scan[1] is None and {"_rowid", "source_rowid"} & set(scan[0])]
    assert whole_key_reads == [], f"the retraction read a whole key column: {whole_key_reads}"
