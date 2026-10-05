"""How the Ray stage job PRODUCES a stage's rows: the media stream, the distributed staging set, and their cleanup.

What a tier write means (create-or-converge, the delta window and its retraction, the marker, the blob field carry,
the declared dataset id) is `service_kit.lakehouse.tier_write`'s, shared with the in-process engine and pinned on both
lanes by `tests/integration/test_both_engines_land_a_stage_through_one_write.py`. This file holds only the Ray-lane
producer properties that write cannot see.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import cast

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


def _bronze_media(tmp_path: Path, png_bytes: bytes, rows: int) -> str:
    import lance

    src = str(tmp_path / "bronze_stream")
    table = pa.table(
        {"id": pa.array(list(range(rows)), pa.int64()), "payload": blob_array([png_bytes] * rows)},
        schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
    )
    lance.write_dataset(table, src, data_storage_version="2.2", enable_stable_row_ids=True)
    return src


def test_the_media_producer_streams_bounded_slices_and_derives_every_row(tmp_path: Path, png_bytes: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the driver holds is bounded by the SLICE, not the run, and the output is what one pass would have written.

    Measured at `_media_batch`, the one seam every slice takes: a slice of N rows means N rows' payloads, pylists,
    thumbnails and embeddings were live at once (ray-project's `patterns/generators.rst`: yield in chunks). A smaller
    peak is worthless if the output changed, so the same run asserts rows, order, root provenance, blob typing and the
    derived columns on every row, not only the first slice's.
    """
    import lance

    src = _bronze_media(tmp_path, png_bytes, rows=7)
    src_rowids = lance.dataset(src).to_table(with_row_id=True).column("_rowid").to_pylist()
    monkeypatch.setattr(job, "MEDIA_BATCH_ROWS", 2)
    widths: list[int] = []
    real_batch = job._media_batch

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        out = real_batch(*args, **kwargs)
        widths.append(out.num_rows)
        return out

    monkeypatch.setattr(job, "_media_batch", spy)
    dst = str(tmp_path / "silver_stream")
    job._run_stage(src, dst, "silver-media", {}, lineage='{"run_id": "r1"}')

    assert max(widths) <= 2, f"the driver materialised {max(widths)} rows at once with a slice of 2 — it is not streaming"
    out = lance.dataset(dst)
    assert blobs.blob_field_names(out.schema) == ["payload"], "blob-v2 typing was demoted"
    got = out.to_table(columns=["id", "source_rowid", "stage", "thumbnail", "embedding"]).sort_by("id")
    assert got.column("id").to_pylist() == list(range(7)), "rows were lost or duplicated across slices"
    assert got.column("source_rowid").to_pylist() == src_rowids
    assert set(got.column("stage").to_pylist()) == {"silver-media"}
    assert all(t is not None for t in got.column("thumbnail").to_pylist()), "a later slice skipped derivation"
    assert all(e is not None for e in got.column("embedding").to_pylist())


def test_an_empty_media_source_still_creates_the_target(tmp_path: Path) -> None:
    """Zero slices would leave the target ABSENT, and an absent dataset is not the same answer as an empty one to the
    tier's readers: the empty slice's schema creates it with every column it would carry."""
    import lance

    src = str(tmp_path / "bronze_empty")
    lance.write_dataset(
        pa.table({"id": pa.array([], pa.int64()), "payload": blob_array([])}, schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")])),
        src,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )

    dst = str(tmp_path / "silver_empty")
    job._run_stage(src, dst, "silver-media", {})

    out = lance.dataset(dst)
    assert out.count_rows() == 0 and out.has_stable_row_ids
    assert "stage" in out.schema.names and "source_rowid" in out.schema.names


def test_a_second_media_hop_neither_duplicates_the_artifacts_nor_keeps_reshaping(tmp_path: Path, png_bytes: bytes) -> None:
    """A tier that already carries `thumbnail`/`embedding` carries them forward rather than deriving a second pair.

    Without the guard the second hop appended a duplicate column and died `LanceError(Schema): Duplicate field name
    "thumbnail"`. Shape STABILITY is asserted from the second hop on: the head mints `source_rowid`, so bronze's shape
    and silver's cannot be equal.
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

    job._run_stage(src, silver, "silver", {}, lineage='{"run_id": "r-silver"}')
    job._run_stage(silver, gold, "gold", {}, lineage='{"run_id": "r-gold"}')
    job._run_stage(gold, platinum, "platinum", {}, lineage='{"run_id": "r-platinum"}')

    up, out, again = lance.dataset(silver), lance.dataset(gold), lance.dataset(platinum)
    assert sorted(out.schema.names) == sorted(up.schema.names)  # same columns, derived exactly once
    assert len(out.schema.names) == len(set(out.schema.names))  # and no second `thumbnail`
    assert again.schema.equals(out.schema), "the lane is still reshaping the tier hop after hop"
    assert out.to_table(columns=["stage"]).column("stage").to_pylist() == ["gold", "gold"]
    # Lance normalises the JSONB text, so the document is compared parsed rather than byte-wise.
    assert [json.loads(cell) for cell in out.to_table(columns=["lineage"]).column("lineage").to_pylist()] == [{"run_id": "r-gold"}] * 2
    assert out.to_table(columns=["thumbnail"]).column("thumbnail").to_pylist() == up.to_table(columns=["thumbnail"]).column("thumbnail").to_pylist()


def _bronze_tabular(tmp_path: Path, rows: int = 4) -> str:
    """Bronze exactly as ``medallion.services.compute.seed_bronze`` writes it: `stage` sits third, ahead of the
    `source_rowid` the first stage mints, which is what makes a re-stamp's column positions matter from silver on."""
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


@pytest.fixture
def isolated_ray(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A PRIVATE in-process Ray for the distributed producer — never the estate's live cluster.

    `ray.init()` with no address ADOPTS whichever cluster it discovers (measured on this host: four live instances,
    the k3s KubeRay head among them), so `address="local"` forces a fresh one. The uv runtime-env hook is off because
    it packages the working directory into every worker, which fails outright when pytest's cwd is not the project root.
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
    """The distributed producer on a real Ray with the real lance_ray write, silver to gold.

    `slow` because it starts a Ray cluster (measured 8.4 s to initialise on this host).
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


def test_the_distributed_producer_creates_its_staging_set_from_the_SAME_construction_it_emits(tmp_path: Path) -> None:
    """The staging set's schema and the block the real `map_batches` lambda emits must agree on column ORDER.

    `lance_ray` appends by casting each block to the staging set positionally, so two constructions of one schema can
    only agree by luck; a re-stamp (silver already carrying `stage` and `lineage`, which is silver->gold) is the case
    where a rebuilt schema and the in-place stamp diverge. `lance_ray` is stubbed to capture the schema the staging set
    is created with and the block the production lambda emits for a real upstream batch; its append is real, so the
    write and its contract run over what the lambda produced.
    """
    import lance

    local_job = _load_job()
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
        """Enough of `lance_ray` to drive the real producer: read, map, append what the lambda produced."""

        def read_lance(self, uri: str, **_kw):  # noqa: ANN003, ANN202
            return _Blocks(lance.dataset(uri).to_table())

        def write_lance(self, blocks, uri: str, **_kw) -> None:  # noqa: ANN001, ANN003
            real_write(blocks.table, uri, mode="append", data_storage_version="2.2")

    real_write = lance.write_dataset

    def _capture_write(data, uri, **kw):  # noqa: ANN001, ANN003, ANN202
        if str(uri).startswith(dst) and kw.get("mode") == "overwrite":
            created["schema"] = data.schema
        return real_write(data, uri, **kw)

    # `sys.modules` is typed `dict[str, ModuleType]`, and the stub answers the two attributes the job imports.
    sys.modules["lance_ray"] = cast("ModuleType", _LanceRay())
    try:
        local_job.lance.write_dataset = _capture_write  # the job holds its own `lance` reference
        local_job._run_stage(from_uri=src, to_uri=dst, stage="gold", lineage='{"run":"r2"}', so={})
    finally:
        local_job.lance.write_dataset = real_write
        sys.modules.pop("lance_ray", None)

    assert created.get("schema") is not None, "the distributed producer never created its staging set"
    assert emitted.get("schema") is not None, "the production map_batches lambda never ran"
    assert created["schema"].names == emitted["schema"].names, (
        f"the staging schema and the emitted block disagree on column ORDER: staging={created['schema'].names} emitted={emitted['schema'].names}"
    )


def test_a_declared_cardinality_the_job_cannot_honour_is_refused_at_DECLARATION_time() -> None:
    """Refused at the door, not at 3am on the cluster: the write refuses an unknown cardinality too, but by then a job
    has been submitted and the operator sees a stage FAIL instead of a 422."""
    import pydantic

    from service_kit.lakehouse.transform_specs import TransformSpec

    with pytest.raises(pydantic.ValidationError):
        TransformSpec(name="x", project="acme", from_id="a", to_id="b", task="stage-transform", cardinality="one-to-many")


def test_the_submit_path_FORWARDS_the_declared_cardinality_to_the_job(tmp_path: Path) -> None:
    """A project's declared fan-out reaches the order's stamp, which the job reads as `RASK_CARDINALITY`.

    Driven through the stage lane's order builder over a REAL declaration on `tmp_path` (`to_env()` is pinned by
    `tests/unit/test_the_submitter_and_the_job_agree_on_the_wire.py`).
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

    assert order.stamp.cardinality == ONE_TO_MANY, "the declared fan-out did not reach the order's stamp"
    assert order.to_env()["RASK_CARDINALITY"] == ONE_TO_MANY


def test_a_staging_set_that_cannot_be_dropped_SAYS_SO(capsys: pytest.CaptureFixture[str]) -> None:
    """Cleanup never raises, and never fails silently either.

    `_drop_staged` runs after the write landed, so a propagating error would turn a landed stage into a reported
    failure and invite a re-run of finished work; swallowing without a word would leave an orphaned Lance dataset under
    the destination that the `_staging` control prefix hides from the maintenance walk.
    """
    job._drop_staged("s3://nowhere/never-written/_staging/run-1", {"endpoint": "http://127.0.0.1:1"})

    printed = capsys.readouterr().out
    assert "staging" in printed.lower(), f"an undroppable staging set left no trace: {printed!r}"
    assert "_staging/run-1" in printed, printed
