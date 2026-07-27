"""Smoke test: chain PageLoaderActor -> FakeAltoActor -> AltoWriterActor via Ray Data."""

from pathlib import Path

import pytest


@pytest.fixture
def tmp_buckets(tmp_path: Path):
    src = tmp_path / "in"
    sink = tmp_path / "out"
    src.mkdir()
    sink.mkdir()
    from PIL import Image

    for i in range(2):
        img = Image.new("RGB", (200, 100), color=(255, 255, 255))
        img.save(src / f"page{i}.jpg")
    return src, sink


# slow: brings up a local Ray instance and runs a real Ray Data pipeline — tens of minutes
# on a shared box (this test class was the bulk of the pre-seal "~32 min" suite). Fake model,
# real runtime: exactly the long-runtime contract the slow marker documents.
@pytest.mark.slow
def test_fake_pipeline_via_ray_data(tmp_buckets):
    """Test that actors compose under ray.data.map_batches without a full cluster."""
    pytest.importorskip("ray", minversion="2.0")

    src_dir, sink_dir = tmp_buckets

    import ray
    import ray.data

    from htr.actors.fake import FakeAltoActor
    from htr.actors.io import AltoWriterActor, PageLoaderActor
    from storage import FSSink, FSSource

    # Start Ray with enough CPUs for the actor pool. The pipeline has 3 stages
    # each with concurrency=1 + the driver, so num_cpus must be >= 4 — with fewer
    # CPUs the streaming executor deadlocks waiting on actor placement.
    #
    # When a persistent cluster is already running (e.g. `make ray-up` for the
    # dev dashboard), Ray refuses num_cpus: `ValueError: num_cpus and num_gpus
    # must not be provided when connecting to an existing cluster`. Detect that
    # case and attach without resource overrides — the existing cluster has way
    # more than 4 CPUs in dev, so the deadlock can't trigger.
    if not ray.is_initialized():
        try:
            ray.init(ignore_reinit_error=True, num_cpus=4, include_dashboard=False)
        except ValueError as e:
            if "num_cpus and num_gpus must not be provided" not in str(e):
                raise
            ray.init(ignore_reinit_error=True, include_dashboard=False)

    try:
        src = FSSource(root=src_dir)
        sink = FSSink(root=sink_dir)
        keys = sorted(src.keys())
        assert len(keys) == 2

        ds = ray.data.from_items([{"key": k} for k in keys])
        ds = ds.map_batches(PageLoaderActor, fn_constructor_kwargs={"source": src}, batch_size=8, concurrency=1)
        ds = ds.map_batches(FakeAltoActor, batch_size=8, concurrency=1)
        ds = ds.map_batches(AltoWriterActor, fn_constructor_kwargs={"sink": sink}, batch_size=8, concurrency=1)
        ds.materialize()

        written = sorted(p.name for p in sink_dir.iterdir())
        assert written == ["page0.xml", "page1.xml"]
        for w in written:
            assert b"<alto" in (sink_dir / w).read_bytes()
    finally:
        ray.shutdown()
