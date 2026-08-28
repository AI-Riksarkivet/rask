"""The annotations package's contracts, read wire AND write plane.

Read side: the Arrow-IPC serialization the frontend ``tableFromIPC`` depends on
(the routes themselves are proven E2E). Write side: save deltas + merge_insert
atomicity, tag rows/idempotency/arity, author stamping, version history +
checkout, schema single-source, and the OpenLineage save event.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import lance
import pyarrow as pa
import pytest
from annotator.annotations.save import build_delta, new_rows
from annotator.annotations.schema import EMPTY_SCHEMA, NewAnnotation, TagWrite
from annotator.annotations.tags import check_keys_arity, tag_id, tag_rows
from annotator.annotations.wire import ipc_stream

from service_kit.lancekit.descriptor import Declared


_MEDIA_DECLARED = Declared.model_validate({"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}})

if TYPE_CHECKING:
    from pathlib import Path


def _read_ipc(raw: bytes) -> pa.Table:
    with pa.ipc.open_stream(pa.BufferReader(raw)) as reader:
        return reader.read_all()


def test_ipc_stream_roundtrips() -> None:
    tbl = pa.table(
        {
            "id": ["x"],
            "x": pa.array([1.0], pa.float32()),
            "shape_type": ["rectangle"],
        }
    )
    back = _read_ipc(ipc_stream(tbl))
    assert back.num_rows == 1
    assert back.column("id")[0].as_py() == "x"


def test_empty_schema_is_a_parseable_empty_stream() -> None:
    # A dataset with no annotations table degrades to this — the client must still
    # parse it and render 0, so it carries the columns ArrowDataPlugin reads.
    back = _read_ipc(ipc_stream(EMPTY_SCHEMA.empty_table()))
    assert back.num_rows == 0
    names = set(back.schema.names)
    # geometry the PixiJS ArrowDataPlugin reads
    assert {"x", "y", "width", "height", "polygon", "shape_type", "status", "mask"} <= names
    # active-learning columns (the review queue ranks by these)
    assert {"confidence", "uncertainty", "source", "model_version"} <= names
    # the settle-while-empty lifecycle columns (merge handoff Q1) — tz-aware UTC µs
    for col in ("created_at", "updated_at"):
        assert EMPTY_SCHEMA.field(col).type == pa.timestamp("us", tz="UTC")
    # the four training columns are DELIBERATELY absent until the training loop
    # defines them (handoff Q2) — pin the decision so an accidental guess fails here
    assert not {"trained_in_version", "margin", "logits", "encoder_embedding"} & names


def test_tag_rows_stamp_lifecycle_timestamps_at_row_birth() -> None:
    # Insert-only tag rows are born with created_at == updated_at (both server-stamped);
    # existing rows are never touched by the tag write, so birth is the only stamp.
    tbl = tag_rows(
        [TagWrite(doc_id="d1", keys=[0, 19], labels=["speech"])],
        _MEDIA_DECLARED,
        author="gabriel",
        schema=_full_schema(),
    )
    row = tbl.to_pylist()[0]
    assert row["created_at"] is not None and row["created_at"] == row["updated_at"]


def _ann_table() -> pa.Table:
    """Three annotations: a prediction with a polygon, an accepted one, another prediction."""
    return pa.table(
        {
            "id": ["a", "b", "c"],
            "status": ["prediction", "accepted", "prediction"],
            "label": ["text-line", "figure", "text-line"],
            "text": ["foo", "", "bar"],
            "x": pa.array([1.0, 2.0, 3.0], pa.float32()),
            "polygon": pa.array([[0.0, 0.0, 1.0, 1.0], [], [2.0, 2.0]], pa.list_(pa.float32())),
        }
    )


def test_build_delta_patches_only_editable_fields_and_carries_geometry() -> None:
    current = _ann_table()
    delta = build_delta(current, {"a": {"status": "accepted"}, "c": {"label": "heading"}})
    # only the two edited rows are in the delta, matched by id
    assert delta.num_rows == 2
    by_id = {r["id"]: r for r in delta.to_pylist()}
    assert by_id["a"]["status"] == "accepted"  # patched
    assert by_id["a"]["polygon"] == [0.0, 0.0, 1.0, 1.0]  # geometry carried forward
    assert by_id["a"]["label"] == "text-line"  # untouched field carried forward
    assert by_id["c"]["label"] == "heading"  # patched
    assert by_id["c"]["x"] == 3.0  # geometry carried forward


def test_geometry_edit_patches_shape_carries_fields() -> None:
    # simulates the save()'s geometry merge: a moved shape patches x/polygon by id
    current = _ann_table()
    edits_by_id: dict[str, dict[str, object]] = {}
    edits_by_id.setdefault("a", {}).update({"x": 9.0, "polygon": [1.0, 1.0, 2.0, 2.0]})
    delta = build_delta(current, edits_by_id)
    r = {x["id"]: x for x in delta.to_pylist()}["a"]
    assert r["x"] == 9.0 and r["polygon"] == [1.0, 1.0, 2.0, 2.0]  # geometry patched
    assert r["status"] == "prediction" and r["label"] == "text-line"  # fields carried forward


def test_merge_insert_save_is_one_atomic_version(tmp_path: Path) -> None:
    uri = str(tmp_path / "annotations.lance")
    lance.write_dataset(_ann_table(), uri)
    ds = lance.dataset(uri)
    v0 = ds.version

    delta = build_delta(ds.to_table(), {"a": {"status": "accepted"}, "b": {"status": "rejected"}})
    ds.merge_insert("id").when_matched_update_all().execute(delta)

    after = lance.dataset(uri)
    got = {r["id"]: r for r in after.to_table().to_pylist()}
    assert got["a"]["status"] == "accepted"  # persisted
    assert got["b"]["status"] == "rejected"  # persisted
    assert got["c"]["status"] == "prediction"  # untouched row unchanged
    assert got["a"]["polygon"] == [0.0, 0.0, 1.0, 1.0]  # geometry survived the round-trip
    assert after.version == v0 + 1  # exactly one new version


def _full_schema() -> pa.Schema:
    """The annotations contract + the chunk identity columns a real table carries."""
    return pa.schema([("doc_id", pa.string()), ("speech_id", pa.int64()), ("chunk_id", pa.int64()), *EMPTY_SCHEMA])


def test_new_rows_stamps_identity_and_carries_geometry() -> None:
    ident = {"doc_id": "d1", "speech_id": 0, "chunk_id": 19}
    tbl = new_rows(
        [NewAnnotation(id="n1", shape_type="polygon", polygon=[1.0, 2.0, 3.0, 4.0], label="line")],
        ident,
        _full_schema(),
    )
    r = tbl.to_pylist()[0]
    assert (r["doc_id"], r["speech_id"], r["chunk_id"]) == ("d1", 0, 19)  # identity stamped
    assert r["id"] == "n1" and r["shape_type"] == "polygon" and r["polygon"] == [1.0, 2.0, 3.0, 4.0]
    assert r["status"] == "accepted" and r["source"] == "human"  # human-drawn defaults
    assert r["confidence"] is None  # unspecified column → null via schema


def test_schema_single_source_of_truth(tmp_path: Path) -> None:
    """The seeded dataset's schema must be EXACTLY identity + the backend EMPTY_SCHEMA.

    seed_annotations.py IMPORTS EMPTY_SCHEMA (no parallel literal), and the engine's
    parallel column list was deleted outright (the frontend is schema-driven off the
    wire) — so the backend contract is the ONE source; this pins the composition."""
    import importlib.util
    from pathlib import Path as P

    spec = importlib.util.spec_from_file_location("seed_annotations", P(__file__).resolve().parents[2] / "scripts" / "seed_annotations.py")
    assert spec is not None and spec.loader is not None
    seed_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_mod)

    uri = seed_mod.seed(str(tmp_path), "a" * 16)
    seeded = lance.dataset(uri).schema
    expected = pa.schema(
        [
            ("doc_id", pa.string()),
            ("speech_id", pa.int64()),
            ("chunk_id", pa.int64()),
            ("frame_idx", pa.int64()),
            *EMPTY_SCHEMA,
        ]
    )
    assert seeded.equals(expected), f"schema drift:\nseeded={seeded}\nexpected={expected}"


# `test_get_author_seam_defaults_to_anon_and_trims` lived here and went with the seam it tested:
# the X-User header no longer chooses the author anywhere. The anon-default property it pinned now
# belongs to `current_subject` and is pinned in `test_annotator_governed_auth.py`.


def test_new_rows_stamps_the_author_as_reviewer() -> None:
    # save_annotations merges the author into the identity stamp; a new row carries it.
    ident = {"doc_id": "d1", "speech_id": 0, "chunk_id": 19, "reviewer": "gabriel"}
    tbl = new_rows([NewAnnotation(id="n1", shape_type="rectangle")], ident, _full_schema())
    assert tbl.to_pylist()[0]["reviewer"] == "gabriel"  # server-stamped, not client-claimed


def test_save_edit_insert_delete_round_trip(tmp_path: Path) -> None:
    schema = _full_schema()
    ident = {"doc_id": "d1", "speech_id": 0, "chunk_id": 19}
    uri = str(tmp_path / "annotations.lance")
    lance.write_dataset(
        pa.Table.from_pylist(
            [{**ident, "id": "a", "status": "prediction"}, {**ident, "id": "b", "status": "accepted"}],
            schema=schema,
        ),
        uri,
    )
    ds = lance.dataset(uri)
    v0 = ds.version

    # edit `a` + insert new shape `c` — ONE merge_insert (update + insert)
    delta = pa.concat_tables(
        [
            build_delta(ds.to_table(), {"a": {"status": "accepted"}}),
            new_rows([NewAnnotation(id="c", shape_type="rectangle", x=9.0)], ident, schema),
        ]
    )
    ds.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(delta)
    after = lance.dataset(uri)
    got = {r["id"]: r for r in after.to_table().to_pylist()}
    assert got["a"]["status"] == "accepted"  # edit applied
    assert got["c"]["shape_type"] == "rectangle" and got["c"]["x"] == 9.0  # insert applied
    assert got["c"]["doc_id"] == "d1"  # new shape carries the unit identity
    assert after.version == v0 + 1  # edit + insert = one atomic version

    # delete `b`
    after.delete("id IN ('b')")
    final = lance.dataset(uri)
    assert {r["id"] for r in final.to_table().to_pylist()} == {"a", "c"}
    assert final.version == v0 + 2


def test_tag_id_is_deterministic_namespaced_and_collision_safe() -> None:
    a = tag_id("d1", [0, 19], "speech")
    assert a == tag_id("d1", [0, 19], "speech")  # deterministic → re-tag is idempotent
    assert a.startswith("tag:")  # the guard: never equals a drawn shape's random id
    assert tag_id("d1", [0, 19], "a:b") != tag_id("d1", [0, 19], "a")  # label sep can't collide
    assert tag_id("d1", [0, 19], "x") != tag_id("d1", [0, 20], "x")  # per-chunk


def test_tag_rows_stamp_identity_shape_and_author() -> None:
    tbl = tag_rows(
        [TagWrite(doc_id="d1", keys=[0, 19], labels=["speech", "music"])],
        _MEDIA_DECLARED,
        author="gabriel",
        schema=_full_schema(),
    )
    rows = tbl.to_pylist()
    assert len(rows) == 2  # one row per label
    r = rows[0]
    assert (r["doc_id"], r["speech_id"], r["chunk_id"]) == ("d1", 0, 19)  # per-row identity
    assert r["shape_type"] == "tag" and r["label"] == "speech"  # discriminator + value
    assert r["source"] == "human" and r["status"] == "accepted"  # mode-blind human provenance
    assert r["reviewer"] == "gabriel"  # server author, not client-claimed
    assert (r["x"], r["y"], r["width"], r["height"]) == (0.0, 0.0, 0.0, 0.0)  # geometry zeroed
    assert r["confidence"] is None  # unlisted column → null
    assert r["id"] == tag_id("d1", [0, 19], "speech")  # deterministic id


def test_tag_rows_dedupes_duplicate_labels() -> None:
    # A batch may repeat a chunk+label; Lance merge_insert would insert both identical-id
    # rows, so tag_rows must dedup (else idempotency breaks).
    tbl = tag_rows(
        [
            TagWrite(doc_id="d1", keys=[0, 19], labels=["cat", "cat"]),
            TagWrite(doc_id="d1", keys=[0, 19], labels=["cat"]),
        ],
        _MEDIA_DECLARED,
        author="gabriel",
        schema=_full_schema(),
    )
    assert tbl.num_rows == 1  # one row for the single distinct (chunk, label)


def test_check_keys_arity_rejects_mismatched_client_keys() -> None:
    # keys pair POSITIONALLY with the descriptor's non-doc identity fields; a short
    # list would stamp NULL identity columns (rows no chunk filter ever matches) and
    # a long one silently drops keys — both must 400 at the boundary, before any write.
    from service_kit.exceptions import ValidationError

    ok = [TagWrite(doc_id="d1", keys=[0, 19], labels=["x"])]
    check_keys_arity(_MEDIA_DECLARED, ok)  # correct arity passes
    for bad_keys in ([], [0], [0, 19, 7]):
        with pytest.raises(ValidationError, match="arity"):
            check_keys_arity(_MEDIA_DECLARED, [TagWrite(doc_id="d1", keys=bad_keys, labels=["x"])])


def _insert_only(ds: lance.LanceDataset, delta: pa.Table) -> None:
    """The tag write's semantic: insert unmatched only (matched rows left as-is)."""
    ds.merge_insert("id").when_not_matched_insert_all().execute(delta)


def test_tag_merge_insert_is_multi_unit_one_version_and_shape_safe(tmp_path: Path) -> None:
    schema = _full_schema()
    ident = {"doc_id": "d1", "speech_id": 0, "chunk_id": 19}
    uri = str(tmp_path / "annotations.lance")
    # a drawn shape already lives on the chunk
    lance.write_dataset(
        pa.Table.from_pylist([{**ident, "id": "a1", "shape_type": "rectangle", "x": 5.0}], schema=schema),
        uri,
    )
    ds = lance.dataset(uri)
    v0 = ds.version

    # tag TWO different chunks in ONE insert-only merge
    delta = tag_rows(
        [
            TagWrite(doc_id="d1", keys=[0, 19], labels=["speech"]),
            TagWrite(doc_id="d1", keys=[0, 20], labels=["music"]),
        ],
        _MEDIA_DECLARED,
        author="gabriel",
        schema=schema,
    )
    _insert_only(ds, delta)
    after = lance.dataset(uri)
    got = {r["id"]: r for r in after.to_table().to_pylist()}
    assert got["a1"]["x"] == 5.0  # drawn shape untouched — a tag: id never matches it
    assert got[tag_id("d1", [0, 19], "speech")]["shape_type"] == "tag"
    assert got[tag_id("d1", [0, 20], "music")]["chunk_id"] == 20  # the 2nd unit's tag
    assert after.version == v0 + 1  # both units → one atomic version

    # idempotent: re-applying the same tags adds no rows
    before = after.count_rows()
    _insert_only(after, delta)
    assert lance.dataset(uri).count_rows() == before


def test_tag_resave_preserves_human_review(tmp_path: Path) -> None:
    # A reviewer edits a tag row (rejects it); a later re-save of the SAME tag must NOT
    # reset it to the workflow default (the insert-only semantic).
    schema = _full_schema()
    ident = {"doc_id": "d1", "speech_id": 0, "chunk_id": 19}
    uri = str(tmp_path / "annotations.lance")
    tid = tag_id("d1", [0, 19], "speech")
    # the tag exists and a human has REJECTED it + renamed the label
    lance.write_dataset(
        pa.Table.from_pylist(
            [{**ident, "id": tid, "shape_type": "tag", "label": "speech-edited", "status": "rejected"}],
            schema=schema,
        ),
        uri,
    )
    ds = lance.dataset(uri)

    # re-save the workflow tag (label="speech", status="accepted") via insert-only
    delta = tag_rows([TagWrite(doc_id="d1", keys=[0, 19], labels=["speech"])], _MEDIA_DECLARED, author="x", schema=schema)
    _insert_only(ds, delta)

    row = {r["id"]: r for r in lance.dataset(uri).to_table().to_pylist()}[tid]
    assert row["status"] == "rejected"  # human review preserved, NOT reset to "accepted"
    assert row["label"] == "speech-edited"  # human rename preserved


def test_iso_timestamp_formats_version_timestamps() -> None:
    from datetime import datetime

    from annotator.annotations.versions import iso_timestamp

    assert iso_timestamp(datetime(2026, 7, 20, 12, 0, 0)) == "2026-07-20T12:00:00"  # datetime → ISO
    assert iso_timestamp("already-a-string") == "already-a-string"
    assert iso_timestamp(None) == ""


def test_version_history_counts_this_units_rows_per_version(tmp_path: Path) -> None:
    # The compare-versions endpoint's core: for each Lance version, count THIS unit's
    # rows via time-travel (checkout) — a different unit's rows must not leak in.
    schema = _full_schema()
    unit = {"doc_id": "d1", "speech_id": 0, "chunk_id": 19}
    other = {"doc_id": "d1", "speech_id": 0, "chunk_id": 99}
    uri = str(tmp_path / "annotations.lance")
    lance.write_dataset(pa.Table.from_pylist([{**unit, "id": "a1"}], schema=schema), uri)  # v1
    ds = lance.dataset(uri)
    ds.merge_insert("id").when_not_matched_insert_all().execute(  # v2: +1 ours, +1 other unit
        pa.Table.from_pylist([{**unit, "id": "a2"}, {**other, "id": "b1"}], schema=schema)
    )
    ds = lance.dataset(uri)
    where = "doc_id = 'd1' AND speech_id = 0 AND chunk_id = 19"
    counts = {int(v["version"]): ds.checkout_version(int(v["version"])).to_table(filter=where, columns=["id"]).num_rows for v in ds.versions()}
    assert counts[1] == 1  # our unit had 1 annotation at v1
    assert counts[2] == 2  # our unit had 2 at v2 — the other unit's row is excluded


def test_checkout_translates_bad_version_to_notfound(tmp_path: Path) -> None:
    from annotator.annotations.versions import checkout

    from service_kit.exceptions import NotFoundError

    uri = str(tmp_path / "annotations.lance")
    lance.write_dataset(pa.Table.from_pylist([{"id": "a1"}], schema=_full_schema()), uri)
    ds = lance.dataset(uri)
    assert checkout(ds, 1).version == 1  # a valid version time-travels
    with pytest.raises(NotFoundError, match="version 999 not found"):
        checkout(ds, 999)  # out-of-range → clean 404, not a raw 500


def test_save_emits_spec_2_0_2_openlineage(tmp_path: Path) -> None:
    from service_kit.lancekit.lineage_emit import build_save_event

    uri = str(tmp_path / "annotations.lance")
    lance.write_dataset(
        pa.Table.from_pylist(
            [{"doc_id": "d1", "speech_id": 0, "chunk_id": 19, "id": "a", "status": "accepted"}],
            schema=_full_schema(),
        ),
        uri,
    )
    ev = build_save_event(ds=lance.dataset(uri), table_uri=uri, table_name="annotations", unit_key="d1/0/19")
    # spec-2-0-2 RunEvent shape
    assert ev["eventType"] == "COMPLETE"
    assert ev["schemaURL"].endswith("2-0-2/OpenLineage.json#/$defs/RunEvent")
    assert ev["producer"].endswith("service-kit")  # the emitting kernel, named honestly since the dissolution
    assert ev["job"]["name"] == "annotate.merge_insert"
    assert ev["run"]["runId"]  # deterministic uuid5
    # media unit in, annotations table out
    assert ev["inputs"] == [{"namespace": "media", "name": "d1/0/19"}]
    out = ev["outputs"][0]
    assert out["namespace"] == "media" and out["name"] == "annotations"
    facets = out["facets"]
    assert {"schema", "outputStatistics", "columnLineage", "dataSource"} <= facets.keys()
    # the schema facet carries the annotation columns; columnLineage is non-empty
    names = {f["name"] for f in facets["schema"]["fields"]}
    assert {"id", "status", "polygon", "x"} <= names
    assert facets["columnLineage"]
