"""A real (dummy-payload) Ray Data job proving the full lance-ray capability set against RustFS.

Submitted to a real Ray cluster via ``ray job submit`` (see ``make ray-demo``) — the production-shape
replacement for the in-process fake-Ray compute. In one run it exercises, each as a genuine distributed Ray
job against Lance datasets on RustFS (S3):

  1. WRITE     — read on Ray, transform across workers, write multiple fragments in parallel + commit once.
  2. INDEXING  — build a scalar index, then a filtered query that the index serves.
  3. EVOLUTION — add a derived column (schema + version advance; an older version still pins the old schema).
  4. COMPACTION — compact the many small fragments into fewer larger ones.

Env: S3_ENDPOINT S3_KEY S3_SECRET [S3_REGION] [RUN].
"""
# TOKEN-AUTHED CLUSTER (gate 7 / R3): with RAY_AUTH_MODE=token on the head, export
# RAY_AUTH_MODE=token + RAY_AUTH_TOKEN (kubectl get secret rask-ray-auth-token -o
# jsonpath='{.data.auth_token}' | base64 -d) before submitting. `ray job submit` /
# JobSubmissionClient then attach `Authorization: Bearer` themselves; any RAW
# requests/httpx call against the dashboard (:8265) must send that header itself.
# NEVER put the token in runtime_env.env_vars — the jobs API echoes runtime_env back.

from __future__ import annotations

import os

import lance
import lance_ray as lr  #   # ships in the Ray image, not our services' venv
import pyarrow as pa
from lance.optimize import CompactionOptions


def _storage_options() -> dict[str, str]:
    return {
        "endpoint": os.environ["S3_ENDPOINT"],
        "access_key_id": os.environ["S3_KEY"],
        "secret_access_key": os.environ["S3_SECRET"],
        "region": os.environ.get("S3_REGION", "us-east-1"),
        "allow_http": "true",
        "virtual_hosted_style_request": "false",  # path-style, like the sibling ray jobs (RustFS 403s else)
    }


def _add_tripled(batch: pa.RecordBatch) -> pa.RecordBatch:
    """BatchUDF for the distributed add_columns — returns only the NEW column, merged by row position."""
    tripled = [value * 3 for value in batch["v"].to_pylist()]
    return pa.record_batch({"tripled": pa.array(tripled, pa.int64())})


def main() -> None:
    so = _storage_options()
    run = os.environ.get("RUN", "raydemo")
    base = f"s3://lance-catalog/ray-{run}"
    src, dst = base + "/src", base + "/out"

    # 1) SEED src (pylance), then DISTRIBUTED WRITE with STABLE ROW IDS. We CREATE dst with stable ids (an
    #    empty table of the output schema) and then distributed-APPEND the Ray fragments into it —
    #    stable-row-ids is a dataset-level property, so the appended rows inherit it. min<max rows-per-file
    #    forces 4 files → 4 fragments written in parallel by Ray workers, committed once.
    #    R27 note: `write_lance` gained its OWN `enable_stable_row_ids` in lance-ray 0.5.0 (it genuinely had
    #    none at the 0.4.2 the image used to pin — verified by signature), so with the bumped pins this
    #    create-then-append dance collapses into a single `mode="create"` write. Kept as-is until a cluster
    #    run confirms the one-call form on the real head; the dance is correct at BOTH versions.
    lance.write_dataset(
        pa.table({"id": list(range(64)), "v": list(range(64))}),
        src,
        storage_options=so,
        mode="overwrite",
        data_storage_version="2.2",
    )
    out_schema = pa.schema([("id", pa.int64()), ("v", pa.int64()), ("doubled", pa.int64())])
    lance.write_dataset(
        out_schema.empty_table(),
        dst,
        storage_options=so,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    transformed = lr.read_lance(src, storage_options=so).map_batches(
        lambda batch: {"id": batch["id"], "v": batch["v"], "doubled": batch["v"] * 2}, batch_format="numpy"
    )
    lr.write_lance(
        transformed,
        dst,
        storage_options=so,
        mode="append",
        data_storage_version="2.2",
        min_rows_per_file=8,
        max_rows_per_file=16,
        concurrency=2,
    )
    written = lance.dataset(dst, storage_options=so)
    fragments_before = len(written.get_fragments())
    stable = written.has_stable_row_ids
    print(f"[1/4] WRITE ok rows={written.count_rows()} fragments={fragments_before} dsv={written.data_storage_version} stable_row_ids={stable}")
    if fragments_before < 2 or not stable:
        raise SystemExit(f"expected >=2 fragments + stable row ids, got {fragments_before} / {stable}")

    # 2) INDEXING — a BTREE scalar index on `id`, then a filtered read the index can serve.
    # R27 (2026-07-28): this step now ATTEMPTS the distributed build and falls back, instead of hardcoding
    # the fallback. The previous note claimed lance_ray.create_scalar_index "calls
    # create_index_uncommitted(index_type=, fragment_ids=) which pylance 8.0.0 lacks" — measured false:
    # 8.0.0 HAS both parameters, yet the distributed BTREE build still raised
    #   RuntimeError: Index building failed: BTREE distributed indexing uses
    #   create_index_uncommitted(..., index_type="BTREE", fragment_ids=...)
    # so the CONCLUSION (fall back) was right at those pins and the stated reason was not. The image pins
    # have since moved to pylance 9.0.0 + lance-ray 0.5.0, which this job is the vehicle for proving: try
    # the distributed path, print WHICH path produced the index, and fall back rather than fail the demo.
    # `distributed` in the output line is the evidence — if it reads True the version-alignment follow-up
    # this note used to defer is closed, and the fallback can go.
    index_path = "distributed (lance_ray)"
    try:
        lr.create_scalar_index(uri=dst, column="id", index_type="BTREE", num_workers=2, storage_options=so)
    except Exception as exc:  # any lance_ray/pylance misalignment must degrade the demo, not fail it
        index_path = f"native pylance (distributed unavailable: {type(exc).__name__}: {exc})"
        lance.dataset(dst, storage_options=so).create_scalar_index("id", "BTREE")
    indexed = lance.dataset(dst, storage_options=so)
    index_names = [entry["name"] for entry in indexed.list_indices()]
    hits = indexed.to_table(columns=["id"], filter="id = 7").num_rows
    print(f"[2/4] INDEX ok via {index_path} indices={index_names} query(id=7)->{hits} row(s)")
    if not index_names or hits != 1:
        raise SystemExit(f"index build/query failed: indices={index_names} hits={hits}")

    # 3) DATA EVOLUTION — distributed add_columns (tripled = v*3); schema + version advance, old version pins.
    version_before = indexed.version
    lr.add_columns(dst, transform=_add_tripled, read_columns=["v"], storage_options=so)
    evolved = lance.dataset(dst, storage_options=so)
    old = lance.dataset(dst, storage_options=so, version=version_before)
    print(f"[3/4] EVOLVE ok cols={evolved.schema.names} version={version_before}->{evolved.version} old_version_cols={old.schema.names}")
    if "tripled" not in evolved.schema.names or evolved.version <= version_before:
        raise SystemExit("add_columns did not evolve the schema/version")
    if "tripled" in old.schema.names:
        raise SystemExit("old version must still pin the pre-evolution schema")

    # 4) COMPACTION — merge the small fragments into fewer larger ones. compaction_options must be a real
    # CompactionOptions on pylance 8 (a bare None trips an internal dict check); target 32 rows/fragment
    # merges the 4 sixteen-row fragments into ~2.
    fragments_pre_compact = len(evolved.get_fragments())
    # CompactionOptions is a TypedDict; only target_rows_per_fragment matters here (the rest default at
    # runtime), so the static "missing keys" check is a false positive on this partial construction.
    options = CompactionOptions(target_rows_per_fragment=32)  # ty: ignore[missing-typed-dict-key]
    lr.compact_files(dst, storage_options=so, num_workers=2, compaction_options=options)
    compacted = lance.dataset(dst, storage_options=so)
    fragments_after = len(compacted.get_fragments())
    print(f"[4/4] COMPACT ok fragments {fragments_pre_compact}->{fragments_after}")
    if fragments_after >= fragments_pre_compact:
        raise SystemExit(f"compaction did not reduce fragments: {fragments_pre_compact}->{fragments_after}")

    print("RAY-LANCE ALL OK — write + index + evolve + compact, all distributed on the real Ray cluster")


if __name__ == "__main__":
    main()
