"""Repeated ingest CONVERGES — the goal's own gates (owner, 2026-08-07).

The defect these close: `lander.py` commits a blind Append and nothing anywhere compared a source
listing against what bronze already holds — nine runs of one fixture prefix measured NINE copies of
each file. The fix is identity + anti-join: `id = sha256(key + version_token)` derived in ONE place
(`ingest.identity`), the enumerate dropping every pair whose id bronze already contains.

Three behaviours, each pinned separately because each fails differently:
  unchanged object -> same id       -> skipped at ENUMERATE (never fetched)
  replaced object  -> new etag      -> new id -> lands as a NEW row, old row stays
  new object       -> no id match   -> lands
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

# Populates the source registry by import-time side effect — the same trap that made an in-cluster
# probe pass while the real emit failed: a bare interpreter has NO kinds registered, and a test
# that forgets this asserts against an empty registry, not the plane.
import ingest.adapters  # noqa: F401
import lance
import pytest
from ingest.identity import unit_id
from ingest.lander import CREATION_FLAGS
from ingest.worker import units_to_table
from ingest.workflow import enumerate_chunks


if TYPE_CHECKING:
    from dapr.ext.workflow import WorkflowActivityContext


# ── the identity itself ────────────────────────────────────────────────────────────────


def test_same_key_same_token_is_the_SAME_id() -> None:
    assert unit_id("s3://b/k", "etag1") == unit_id("s3://b/k", "etag1")


def test_a_replaced_object_gets_a_NEW_id() -> None:
    """The owner's ruling made mutation real: same key, new bytes, new listing etag. The id MUST
    move, or the anti-join skips the replacement forever — the exact silent loss ruled out."""
    assert unit_id("s3://b/k", "etag1") != unit_id("s3://b/k", "etag2")


def test_a_tokenless_id_is_BYTE_IDENTICAL_to_the_historic_derivation() -> None:
    """Snapshot sources keep sha256(key) — so every id this plane ever wrote stays valid and the
    change is additive for existing tables. This is the back-compat contract, pinned."""
    import hashlib

    key = "file:///data/page-0001.tif"
    historic = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    assert unit_id(key, None) == historic


def test_the_NUL_separator_prevents_boundary_collisions() -> None:
    assert unit_id("ab", "c") != unit_id("a", "bc")


# ── the versioned S3 listing ───────────────────────────────────────────────────────────


class _Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_: Any) -> list[dict[str, Any]]:
        return self._pages


class _StubClient:
    """The boto3 surface the listing touches — get_paginator('list_objects_v2') and nothing else."""

    def __init__(self, contents: list[dict[str, str]]) -> None:
        self._contents = contents

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return _Paginator([{"Contents": self._contents}])


def test_s3_versioned_listing_yields_uri_and_UNQUOTED_etag() -> None:
    from service_kit.lakehouse.sources import S3Source

    source = S3Source(
        fs=None,
        bucket="b",
        prefix="p/",
        client=_StubClient(
            [  # type: ignore[arg-type]
                {"Key": "p/one.tif", "ETag": '"abc123"'},
                {"Key": "p/two.tif", "ETag": '"def456"'},
            ]
        ),
    )

    assert list(source.iter_versioned_keys()) == [("s3://b/p/one.tif", "abc123"), ("s3://b/p/two.tif", "def456")]


def test_a_clientless_s3_source_degrades_to_SNAPSHOT_not_an_error(tmp_path: Path) -> None:
    """The documented contract: no client -> (key, None) over the pyarrow listing. Local-dir takes
    the same fallback through `iter_versioned_unit_keys`."""
    from ingest.sources import iter_versioned_unit_keys

    from service_kit.lakehouse.sources import LocalDirSource

    (tmp_path / "a.tif").write_bytes(b"II*\x00a")
    pairs = list(iter_versioned_unit_keys(LocalDirSource(tmp_path)))

    assert len(pairs) == 1
    assert pairs[0][1] is None


# ── the anti-join, end to end through the real enumerate activity ─────────────────────


def _seed_bronze(uri: str, keys: list[str]) -> None:
    """A bronze dataset holding exactly these keys, written through the REAL batch builder — so the
    ids the anti-join meets are the ids the worker writes, not a test's imitation of them."""
    table = units_to_table([(key, b"II*\x00fixture") for key in keys])
    lance.write_dataset(table, uri, **CREATION_FLAGS)


def _enumerate(ctx: WorkflowActivityContext, root: Path, uri: str) -> list[dict[str, Any]]:
    payload = {
        "spec": {
            "run_id": "conv-test",
            "kind": "local-dir",
            "project": "p",
            "dataset": "d",
            "options": {"root": str(root)},
        },
        "dataset_uri": uri,
    }
    chunks = enumerate_chunks(ctx, payload)
    # `enumerate_chunks` may return a compact REFUSAL dict instead of chunks (the unit ceiling and
    # the gRPC dispatch budget). Every test through this helper is well under both, so a dict here
    # means the helper's premise broke — asserted rather than cast, so it says which.
    assert isinstance(chunks, list), f"enumeration was refused: {chunks}"
    return chunks


def test_a_rerun_over_an_UNCHANGED_source_enumerates_NOTHING(activity_ctx: WorkflowActivityContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Condition 1's engine: every key already in bronze -> zero chunks -> the run takes the
    units_total == 0 short-circuit -> COMPLETE, no fetch, no commit, no new version."""
    monkeypatch.setenv("RASK_INGEST_LOCAL_ROOT", str(tmp_path))
    root = tmp_path / "src"
    root.mkdir()
    for name in ("a.tif", "b.tif"):
        (root / name).write_bytes(b"II*\x00" + name.encode())

    from ingest.sources import SourceSpec, build_source, iter_versioned_unit_keys

    spec = SourceSpec(kind="local-dir", project="p", dataset="d", options={"root": str(root)})
    keys = [key for key, _ in iter_versioned_unit_keys(build_source(spec))]
    uri = str(tmp_path / "bronze.lance")
    _seed_bronze(uri, keys)

    assert _enumerate(activity_ctx, root, uri) == [], "every object is already in bronze — enumerating any of them re-fetches and re-lands it"


def test_a_NEW_object_enumerates_EXACTLY_ONE_unit(activity_ctx: WorkflowActivityContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Condition 3's engine: the skip happens at enumerate — proven by the chunk carrying ONE key,
    which is also what makes `units_total` the honest fetch count."""
    monkeypatch.setenv("RASK_INGEST_LOCAL_ROOT", str(tmp_path))
    root = tmp_path / "src"
    root.mkdir()
    for name in ("a.tif", "b.tif"):
        (root / name).write_bytes(b"II*\x00" + name.encode())

    from ingest.sources import SourceSpec, build_source, iter_versioned_unit_keys

    spec = SourceSpec(kind="local-dir", project="p", dataset="d", options={"root": str(root)})
    keys = [key for key, _ in iter_versioned_unit_keys(build_source(spec))]
    uri = str(tmp_path / "bronze.lance")
    _seed_bronze(uri, keys)

    (root / "c.tif").write_bytes(b"II*\x00c")
    chunks = _enumerate(activity_ctx, root, uri)

    # POINTERS, not inline keys (§2.13) — so the skip is proven by resolving the chunk's window
    # against the run's unit manifest, which is where the keys now live.
    from ingest.staging import read_unit_slice

    assert len(chunks) == 1
    assert chunks[0]["count"] == 1
    resolved = read_unit_slice(uri, chunks[0]["run_id"], chunks[0]["offset"], chunks[0]["count"])
    assert next(key for key, _ in resolved).endswith("c.tif")


def test_a_REPLACED_token_enumerates_the_key_AGAIN(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Condition 2's engine, at the seam where tokens exist. Bronze holds (key, etag-1); the listing
    now says etag-2; the pair must survive the anti-join because its id is new. Driven through the
    identity directly — the local-dir adapter cannot produce tokens by contract, and faking one
    through it would test the fake."""
    key = "s3://bucket/page.tif"
    uri = str(tmp_path / "bronze.lance")
    table = units_to_table([(key, b"v1-bytes")], tokens=["etag-1"])
    lance.write_dataset(table, uri, **CREATION_FLAGS)

    existing = set(lance.dataset(uri).to_table(columns=["id"]).column("id").to_pylist())
    assert unit_id(key, "etag-1") in existing, "the seeded row's id must be the derivation under test"
    assert unit_id(key, "etag-2") not in existing, "a replaced object's id matched the old row — the replacement would be skipped forever"


def test_the_etag_COLUMN_records_what_the_identity_used(tmp_path: Path) -> None:
    """The audit half: a row must SAY which version of the source object it witnessed."""
    table = units_to_table([("s3://b/k", b"bytes")], tokens=["etag-9"])

    assert table.column("etag").to_pylist() == ["etag-9"]
    assert table.column("id").to_pylist() == [unit_id("s3://b/k", "etag-9")]


# ── the anti-join must FAIL rather than skip nothing (F12c) ────────────────────────────


def test_an_UNREADABLE_bronze_FAILS_the_enumeration_instead_of_ingesting_everything(
    activity_ctx: WorkflowActivityContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: a bare `except Exception` around the `id` read logged and continued with an EMPTY
    set — and an empty set does not mean "nothing to skip", it means "skip NOTHING".

    So one transient object-store error re-fetched and re-landed every object already in bronze:
    silent full re-duplication of a tier, produced by the error path of the very mechanism that
    exists to prevent duplication. `ensure_dataset` runs immediately before this activity and returns
    this exact location, so an unreadable table is a read failure and never a legitimate absence —
    and raising puts it under ACTIVITY_RETRY, so a blip costs a retry and a permanent failure costs
    the run WITH a reason.
    """
    from ingest.workflow import AntiJoinUnavailable

    monkeypatch.setenv("RASK_INGEST_LOCAL_ROOT", str(tmp_path))
    root = tmp_path / "src"
    root.mkdir()
    for name in ("a.tif", "b.tif"):
        (root / name).write_bytes(b"II*\x00" + name.encode())

    keys = ["file://" + str(root / "a.tif"), "file://" + str(root / "b.tif")]
    uri = str(tmp_path / "bronze.lance")
    _seed_bronze(uri, keys)

    def _unreadable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("connection reset by peer")

    monkeypatch.setattr(lance, "dataset", _unreadable)

    with pytest.raises(AntiJoinUnavailable, match="already holds"):
        _enumerate(activity_ctx, root, uri)


def test_an_EMPTY_bronze_is_not_a_failure(activity_ctx: WorkflowActivityContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The legitimate empty case stays legitimate: `ensure_dataset` creates the table with zero rows,
    so a run against a brand-new dataset must enumerate everything rather than refuse."""
    from ingest.catalog import LocalCatalog
    from ingest.runtime import BRONZE_SCHEMA

    monkeypatch.setenv("RASK_INGEST_LOCAL_ROOT", str(tmp_path))
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.tif").write_bytes(b"II*\x00a")

    uri = str(tmp_path / "bronze.lance")
    LocalCatalog(BRONZE_SCHEMA).ensure_at(uri)

    chunks = _enumerate(activity_ctx, root, uri)

    assert len(chunks) == 1
    assert chunks[0]["count"] == 1
