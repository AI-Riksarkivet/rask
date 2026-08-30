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


def test_media_transform_round_trips_blob_and_derives(tmp_path: Path, png_bytes: bytes) -> None:
    """End-to-end: ``_media_transform`` preserves blob-v2 typing AND appends thumbnail+embedding.

    This is the whole point of the Ray blob path — a plain lance_ray write would demote ``payload`` to
    LargeBinary; the pylance round-trip keeps it a blob-v2 column and derives the image artifacts, exactly
    like the in-process ``compute.transform_stage``.
    """
    import lance

    src = str(tmp_path / "bronze_media")
    table = pa.table(
        {"id": pa.array([0, 1], pa.int64()), "payload": blob_array([png_bytes, png_bytes])},
        schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
    )
    lance.write_dataset(table, src, data_storage_version="2.2", enable_stable_row_ids=True)
    src_rowids = lance.dataset(src).to_table(with_row_id=True).column("_rowid").to_pylist()

    dst = str(tmp_path / "silver_media")
    job._media_transform(src, dst, {}, stage="silver-media")

    out = lance.dataset(dst)
    names = out.schema.names
    assert "thumbnail" in names and "embedding" in names  # image artifacts derived
    assert "stage" in names
    assert blobs.blob_field_names(out.schema) == ["payload"]  # blob-v2 typing PRESERVED (not demoted)
    assert out.count_rows() == 2 and out.has_stable_row_ids
    # Row-level provenance parity with compute._carry_forward: source_rowid minted from the source _rowid.
    assert out.to_table(columns=["source_rowid"]).column("source_rowid").to_pylist() == src_rowids


def test_stamp_stage_mints_source_rowid_at_the_head_and_carries_it_forward() -> None:
    """The tabular map function's provenance logic (the distributed path can't run in the unit venv).

    Head: a batch carrying the reserved ``_rowid`` metacolumn (no ``source_rowid`` yet) MINTS source_rowid
    from it and never persists ``_rowid``. Carried: a batch that already has ``source_rowid`` keeps it
    UNCHANGED (root provenance, not re-set to the parent's _rowid) and still sheds ``_rowid``. Mirrors
    compute._carry_source_rowid + _stamp_stage.
    """
    head = pa.table({"id": [1, 2], "_rowid": pa.array([40, 41], pa.uint64())})
    stamped = job._stamp_stage(head, "bronze")
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
    stamped2 = job._stamp_stage(carried, "silver")
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

    stamped = job._stamp_stage(upstream, "gold", doc)
    assert stamped.column_names.count("lineage") == 1
    assert stamped.schema.field("lineage").type.extension_name == "arrow.json"  # Lance JSONB, not a string
    assert stamped.column("lineage").to_pylist() == [doc, doc]  # THIS run's document, on every row

    bare = job._stamp_stage(upstream, "gold")
    assert "lineage" not in bare.column_names  # no document handed over → the parent's is still dropped


def test_media_transform_stamps_the_lineage_column(tmp_path: Path, png_bytes: bytes) -> None:
    """R26 on the Ray MEDIA path: the blob round-trip writes the provenance column in its own commit."""
    import lance

    src = str(tmp_path / "src")
    table = pa.table(
        {"id": pa.array([1, 2], pa.int64()), "payload": blob_array([png_bytes, png_bytes])},
        schema=pa.schema([pa.field("id", pa.int64()), blob_field("payload")]),
    )
    lance.write_dataset(table, src, mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)
    doc = '{"run_id": "r-9", "operation": "aggregate_gold"}'

    job._media_transform(src, str(tmp_path / "dst"), {}, stage="gold", lineage=doc)

    out = lance.dataset(str(tmp_path / "dst"))
    assert out.schema.field("lineage").type.extension_name == "arrow.json"
    assert out.to_table(columns=["id"], filter="json_get_string(lineage, 'run_id') = 'r-9'").num_rows == 2


def test_the_script_uses_the_shared_primitives_rather_than_copies() -> None:
    """Identity, not agreement — the property B14 asks for.

    A copy that merely *behaves* the same is what this file used to check. Asserting the script
    references the shared function objects means a reintroduced local copy fails immediately, rather
    than passing until it drifts in a way somebody remembered to assert.
    """
    from service_kit.lakehouse import media as shared_media
    from service_kit.lakehouse.blobs import blob_field_names as shared_blob_field_names

    job = _load_job()

    assert job.media is shared_media
    assert job.blob_field_names is shared_blob_field_names


def test_the_script_declares_no_local_deriver_copy() -> None:
    """The inlined names are GONE, not merely unused — a dormant copy is a copy."""
    job = _load_job()

    for gone in ("_derive_thumbnail", "_derive_embedding", "_is_image", "_open_guarded", "_is_blob_field"):
        assert not hasattr(job, gone), f"{gone} is back — B14 asks for one implementation, not two"


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

    Measured at the write, because every intermediate copy is derived from the same slice — a write
    of N rows means N rows' payloads, pylists, thumbnails and embeddings were all live at once.
    """
    import lance

    src = _bronze_media(tmp_path, png_bytes, rows=7)
    monkeypatch.setattr(job, "MEDIA_BATCH_ROWS", 2)

    widths: list[int] = []
    real_write = lance.write_dataset

    def spy(data, uri, **kwargs):  # noqa: ANN001, ANN202
        widths.append(data.num_rows)
        return real_write(data, uri, **kwargs)

    monkeypatch.setattr(job.lance, "write_dataset", spy)
    job._media_transform(src, str(tmp_path / "silver_stream"), {}, stage="silver-media")

    assert max(widths) <= 2, f"the driver materialised {max(widths)} rows at once with a batch of 2 — it is not streaming"
    assert sum(widths) == 7, f"rows were lost or duplicated across batches: {widths}"


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


def test_a_rerun_overwrites_rather_than_appending_to_the_previous_run(tmp_path: Path, png_bytes: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sharpest hazard the batched write introduces: the FIRST batch must overwrite and the rest
    append, or a second run of the same stage doubles the table instead of replacing it."""
    import lance

    src = _bronze_media(tmp_path, png_bytes, rows=5)
    monkeypatch.setattr(job, "MEDIA_BATCH_ROWS", 2)
    dst = str(tmp_path / "silver_rerun")

    job._media_transform(src, dst, {}, stage="silver-media")
    job._media_transform(src, dst, {}, stage="silver-media")

    assert lance.dataset(dst).count_rows() == 5, "the rerun appended to the previous run's rows"


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


def test_the_distributed_destination_is_created_with_the_schema_the_transform_emits(tmp_path: Path) -> None:
    """The invariant the break violated, checked through the REAL failing frame.

    `lance_ray` appends by casting each block to the destination's schema, so a destination built by
    any construction other than the transform's own can only ever agree by luck. Driven end to end
    from the producer's bronze shape: a real head stage writes silver, then the destination schema
    and one real emitted block are compared, and finally handed to `lance_ray`'s own
    `pd_to_arrow` — the exact frame in the production traceback.
    """
    import lance
    from lance_ray.pandas import pd_to_arrow

    doc = '{"run_id": "r-gold", "operation": "aggregate_gold"}'
    silver = str(tmp_path / "silver_tabular")
    job._run_stage(_bronze_tabular(tmp_path), silver, "silver", {}, lineage='{"run_id": "r-silver"}')

    upstream = lance.dataset(silver)
    assert upstream.schema.names == ["id", "payload", "stage", "source_rowid", "lineage"]

    target = job._target_schema(upstream, "gold", doc)
    emitted = job._stamp_stage(upstream.to_table(), "gold", doc)

    assert emitted.schema.names == target.names, "the destination and the blocks are two constructions again"
    pd_to_arrow(emitted, target)  # what lance_ray does per block; raises on any positional disagreement


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
    is measured in seconds. The invariant it exercises is guarded fast (and without Ray) by
    `test_the_distributed_destination_is_created_with_the_schema_the_transform_emits`; this one is
    what proves the invariant is the one lance_ray actually enforces.
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
        if str(uri) == dst and kw.get("mode") == "overwrite":
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
