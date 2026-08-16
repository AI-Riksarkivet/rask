"""The whole chain, driven through the REAL activity functions — A3, A4, A6, A7.

This is the piece that turns "the parts work" into "the run works". It calls the same
`enumerate_chunks` / `publish_units` / `finalize` / `emit_*` functions Dapr calls, against real NATS
and real Lance, with only the workflow's own orchestration replaced by driving the steps in order.

Why not run the Dapr workflow engine itself here: that needs a daprd sidecar and a placement service,
which is A11's in-cluster job. What CAN be proven without a cluster is that every activity does what
the workflow expects of it — and that is where the bugs have actually been (four API mistakes so far,
every one found by executing rather than reading).

Skips (never fakes) without NATS: `kubectl port-forward svc/rask-nats 4222:4222`.
"""

from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import lance
import pytest
from dapr.ext.workflow import WorkflowActivityContext
from ingest import lineage as lineage_mod
from ingest.adapters import register_builtin_sources
from ingest.workflow import ChunkResult, ChunkSpec, RunSpec, emit_start, emit_terminal, enumerate_chunks


NATS_URL = os.getenv("RASK_NATS_URL", "nats://localhost:4222")


def _reachable() -> bool:
    p = urlparse(NATS_URL)
    try:
        with socket.create_connection((p.hostname or "localhost", p.port or 4222), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _fresh() -> None:
    register_builtin_sources()
    lineage_mod.reset_events()


def _fixtures(root: Path, count: int) -> None:
    for i in range(count):
        (root / f"page-{i:03d}.tif").write_bytes(b"II*\x00" + f"page-{i}".encode())


def _empty_bronze(path: Path) -> str:
    """The dataset `ensure_dataset` would have created, at the location it would have vended.

    `enumerate_chunks` REFUSES to run against a table it cannot read (F12c): the anti-join's empty
    set means "skip nothing", so degrading to it re-lands every object bronze already holds. The
    activity that precedes it in the workflow creates this dataset, so these tests create it too
    rather than handing the activity a path that does not exist.
    """
    from ingest.catalog import LocalCatalog
    from ingest.runtime import BRONZE_SCHEMA

    uri = str(path)
    LocalCatalog(BRONZE_SCHEMA).ensure_at(uri)
    return uri


def test_enumerate_produces_chunks_not_units(activity_ctx: WorkflowActivityContext, tmp_path: Path) -> None:
    """The decision that dissolved the tracker: activities carry CHUNKS.

    A million persisted-and-replayed activity results would melt the state store. One child per
    ~1000 keys returns one compact result, and the workflow's own durable state becomes the ledger.
    """
    source = tmp_path / "src"
    source.mkdir()
    _fixtures(source, 5)
    # The source tree and the dataset are SIBLINGS: `local-dir` rglobs its root, so a bronze dataset
    # inside it would enumerate its own Lance files as units.
    spec = RunSpec(run_id="r1", kind="local-dir", project="p", dataset="pages", options={"root": str(source)})

    chunks = enumerate_chunks(activity_ctx, {"spec": spec.model_dump(), "dataset_uri": _empty_bronze(tmp_path / "bronze.lance")})

    assert isinstance(chunks, list), f"enumeration was refused: {chunks}"
    assert len(chunks) == 1, "5 units must be ONE chunk, not five activity results"
    parsed = ChunkSpec.model_validate(chunks[0])
    # A chunk is a POINTER now (§2.13): it names its window into the run's unit manifest rather than
    # carrying 5 keys inline, which is what took the workflow's payloads from O(units) to O(chunks).
    assert parsed.expected_units == 5
    assert parsed.keys == [], "the descriptor must not carry the keys any more"
    from ingest.staging import read_unit_slice

    assert len(read_unit_slice(parsed.dataset_uri, parsed.run_id, parsed.offset, parsed.count)) == 5
    assert parsed.run_id == "r1"


def test_enumeration_slices_at_the_chunk_boundary(activity_ctx: WorkflowActivityContext, tmp_path: Path) -> None:
    from ingest import workflow as wf_mod

    source = tmp_path / "src"
    source.mkdir()
    _fixtures(source, 7)
    original = wf_mod.CHUNK_SIZE
    wf_mod.CHUNK_SIZE = 3
    try:
        spec = RunSpec(run_id="r2", kind="local-dir", project="p", dataset="d", options={"root": str(source)})
        chunks = enumerate_chunks(activity_ctx, {"spec": spec.model_dump(), "dataset_uri": _empty_bronze(tmp_path / "bronze.lance")})
    finally:
        wf_mod.CHUNK_SIZE = original

    assert [ChunkSpec.model_validate(c).expected_units for c in chunks] == [3, 3, 1]
    # …and the windows tile the manifest exactly once, which is the property the boundary test is for.
    assert [(ChunkSpec.model_validate(c).offset, ChunkSpec.model_validate(c).count) for c in chunks] == [(0, 3), (3, 3), (6, 1)]
    ids = [ChunkSpec.model_validate(c).chunk_id for c in chunks]
    assert len(set(ids)) == 3, "chunk ids must be unique — the drained event keys off them"


def test_a6_lineage_start_precedes_the_work_and_a_terminal_always_follows(activity_ctx: WorkflowActivityContext, tmp_path: Path) -> None:
    """A6: START at accept, terminal always — the FAIL branch the medallion never had.

    `iiif_produce.py` contains zero `event_type="FAIL"`: a harvest that raised became a 400 with NO
    lineage record, so a failed run was indistinguishable from one that never happened.
    """
    spec = RunSpec(run_id="r3", kind="local-dir", project="p", dataset="d", options={"root": str(tmp_path)})

    emit_start(activity_ctx, spec.model_dump())
    emit_terminal(
        activity_ctx,
        {"spec": spec.model_dump(), "outcome": {"status": "FAILED", "committed_version": None, "rows": 0, "errors": {"k": "boom"}}},
    )

    kinds = [e.event_type for e in lineage_mod.recorded_events()]
    assert kinds == ["START", "FAIL"], f"expected START then FAIL, got {kinds}"


def test_lineage_never_raises_into_the_run(activity_ctx: WorkflowActivityContext) -> None:
    """I8: lineage is OBSERVATIONAL — a broken record must not fail a run that delivered data."""
    rec = lineage_mod.LineageRecorder()
    rec.terminal("r", "COMPLETE", 2, 10, {})
    assert lineage_mod.recorded_events()[-1].event_type == "COMPLETE"


@pytest.mark.skipif(not _reachable(), reason=f"no NATS at {NATS_URL}")
@pytest.mark.asyncio
async def test_a4_a7_the_full_chain_lands_rows_and_commits_once(activity_ctx: WorkflowActivityContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """enumerate -> publish -> drain -> finalize, through the real activities.

    A7 in miniature: exactly ONE data-visibility commit per run. A4: a second run appends rather
    than destroying the first.
    """
    from ingest.catalog import LocalCatalog
    from ingest.queue import WorkQueue
    from ingest.runtime import BRONZE_SCHEMA, dataset_uri, finalize_run, publish_chunk_units
    from ingest.worker import Worker

    warehouse = tmp_path / "wh"
    monkeypatch.setenv("RASK_INGEST_WAREHOUSE", str(warehouse))
    monkeypatch.setenv("RASK_NATS_URL", NATS_URL)

    fixtures = tmp_path / "src"
    fixtures.mkdir()
    _fixtures(fixtures, 4)

    run = f"r{uuid.uuid4().hex[:8]}"
    spec = RunSpec(run_id=run, kind="local-dir", project="p", dataset="pages", options={"root": str(fixtures)})

    uri = dataset_uri(spec)
    LocalCatalog(BRONZE_SCHEMA).ensure_at(uri)
    monkeypatch.setenv("RASK_INGEST_ACTIVE_DATASET", uri)

    chunks = enumerate_chunks(activity_ctx, {"spec": spec.model_dump(), "dataset_uri": _empty_bronze(tmp_path / "bronze.lance")})
    assert isinstance(chunks, list), f"enumeration was refused: {chunks}"
    chunk = ChunkSpec.model_validate(chunks[0])
    assert await publish_chunk_units(chunk) == 4

    class _FileFetcher:
        async def fetch(self, key: str) -> bytes:
            return Path(key.replace("file://", "")).read_bytes()

    queue = await WorkQueue.connect(NATS_URL)
    try:
        outcome = await Worker(queue, _FileFetcher(), name="w1").drain_chunk(run, chunk.chunk_id, 4, uri)
    finally:
        await queue.close()

    assert outcome.units_done == 4
    before = lance.dataset(uri).version
    result = finalize_run(spec, ChunkResult(chunk_id=chunk.chunk_id, fragments=outcome.fragments).fragments, {})

    assert result["rows"] == 4
    assert result["status"] == "COMPLETE"

    # A7, asserted as the DATA property rather than as version arithmetic. `version == before + 1`
    # counted every commit, and the lander deliberately makes more than one: the first commit on a
    # dataset also creates the `partition_key` BITMAP and `id` scalar indexes, and each of those is
    # its own Lance commit ("CREATE ONCE, MAINTAIN ELSEWHERE" — lander.py). So a first run legitimately
    # moves 1 -> 4, and only ONE of those three added rows.
    #
    # This assertion passed for years for the WRONG REASON: `dataset_uri` composed the pre-namespace
    # path, so the fragments were staged under `<wh>/p/` and committed under `<wh>/p-bronze/`. The
    # index creation then failed on a missing data file, was swallowed by lander's never-fatal
    # contract ("could not ensure the scalar indexes — queries will scan"), and left exactly one
    # commit for the arithmetic to find. Fixing the path revealed the assertion had been measuring
    # index FAILURE, not commit discipline.
    dataset = lance.dataset(uri)
    # `total_rows` in the version metadata is CUMULATIVE, so a commit is data-visible only where it
    # DIFFERS from the version before it. Index commits carry the row count forward unchanged, which
    # is exactly what makes this able to tell the two kinds apart.
    history = sorted(((v["version"], int(v["metadata"].get("total_rows", 0))) for v in dataset.versions()), key=lambda vr: vr[0])
    data_commits = [ver for (ver, rows), (_, prev) in zip(history[1:], history[:-1], strict=True) if ver > before and rows != prev]
    assert data_commits == [before + 1], f"a run must produce exactly ONE data-visibility commit, got {data_commits}"
    assert dataset.count_rows() == 4

    # `payload` is a BLOB column, so a written cell is a descriptor and carries no bytes — the same
    # drift `test_worker_queue` had. `read_blobs` is the reader path, and it keys by ROW INDEX.
    blobs = dict(dataset.read_blobs("payload", indices=[0]))
    payload = blobs[0]
    assert payload is not None  # bronze declares payload non-nullable; narrows pylance 10's bytes | None
    assert payload.startswith(b"II*\x00"), "bronze must hold the bytes as received"


def test_completing_with_errors_is_not_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that delivered 9,997 of 10,000 pages did not FAIL.

    Calling it FAILED would either discard good data or teach operators to ignore the status field —
    both worse than a status that says exactly what happened.
    """
    from ingest.catalog import LocalCatalog
    from ingest.runtime import BRONZE_SCHEMA, dataset_uri, finalize_run

    monkeypatch.setenv("RASK_INGEST_WAREHOUSE", str(tmp_path))
    spec = RunSpec(run_id="r9", kind="local-dir", project="p", dataset="d")
    LocalCatalog(BRONZE_SCHEMA).ensure_at(dataset_uri(spec))

    out = finalize_run(spec, [], {"iiif://bad": "corrupt TIFF"})

    assert out["status"] == "COMPLETE_WITH_ERRORS"
    assert out["errors"] == {"iiif://bad": "corrupt TIFF"}
