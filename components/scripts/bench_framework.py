"""Throughput benchmark for the Ray Data HTR pipeline.

Run:
    uv run python bench_framework.py --input /home/morgan/_input/A0060198 \\
        --output /tmp/alto_framework --limit 100
"""

import argparse
import time
from pathlib import Path

import ray

from runner.pipeline import htr_pipeline
from storage import FSSink, FSSource


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=None, help="Process only first N images")
    args = parser.parse_args()

    in_dir = Path(args.input)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.limit:
        import shutil
        import tempfile

        staging = Path(tempfile.mkdtemp(prefix="bench_input_"))
        all_imgs = sorted(in_dir.glob("*.jpg"))[: args.limit]
        for src in all_imgs:
            shutil.copy(src, staging / src.name)
        actual_input = staging
        print(f"Staged {len(all_imgs)} images at {staging}")
    else:
        actual_input = in_dir

    ray.init(ignore_reinit_error=True)

    source = FSSource(root=actual_input)
    sink = FSSink(root=out_dir)
    keys = sorted(source.keys())

    print(f"Benchmark: {len(keys)} images")
    t0 = time.perf_counter()
    ds = htr_pipeline(keys, source, sink)
    n = ds.count()
    dt = time.perf_counter() - t0

    print(f"Done in {dt:.1f}s — {n} ok")
    if dt > 0:
        print(f"Throughput: {n / dt:.2f} img/s")


if __name__ == "__main__":
    main()
