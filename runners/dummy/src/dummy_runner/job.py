"""The dummy mover's job body — CDF delta in, merge_insert out, commit registered.

Shaped like `scripts/ray_stage_job.py`: a script executed to completion, parameterised entirely by
env vars, never shipping code at submit time. "Jobs are scripts baked into images" is the estate's
rule — a transform change is an image rebuild, reproducible by construction.

Env: FROM_URI TO_URI [BASE_VERSION] [RUN_ID] [LINEAGE_JSON]

BASE_VERSION is the delta boundary. It comes from the publication event, which carries the exact
version from the commit/tag-update RESPONSE — never from a DescribeTable read, which could race
ahead and silently skip rows.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import lance
import pyarrow as pa

from dummy_runner.transform import SILVER_SCHEMA, transform_batch

log = logging.getLogger(__name__)


def read_delta(from_uri: str, base_version: int | None) -> pa.Table:
    """Read only the rows added since `base_version` — D1's O(delta), not a tier rescan.

    `_row_created_at_version` is the change-data-feed predicate, verified working in open_ingest.md
    §7.11 row 2. It requires `enable_stable_row_ids` at CREATION — a silent no-op if set later
    (lance_docs/file_format.md:4011-4013), which is why the catalog's creation contract (A14) enforces
    it and why bronze is created empty with the flag rather than on first write.

    base_version None means "everything": a first run has no delta boundary, and an anti-join against
    an empty silver would be the same answer at more cost.
    """
    ds = lance.dataset(from_uri)
    if base_version is None:
        return ds.to_table(with_row_id=True)
    return ds.to_table(with_row_id=True, filter=f"_row_created_at_version > {base_version}")


def write_silver(to_uri: str, rows: pa.Table, run_id: str) -> dict[str, Any]:
    """merge_insert on the stable id — idempotent under redelivery (E2).

    NOT append: a redelivered publication event must converge, not duplicate. merge_insert maps to
    Lance's Update transaction, so concurrent merges are retryable at the application level rather
    than auto-rebased (file_format.md:5155-5159) — which is why the mover holds a per-dataset
    single-flight (E3) and why this function does not try to be clever about concurrency itself.
    """
    try:
        ds = lance.dataset(to_uri)
    except Exception:
        created = lance.write_dataset(rows, to_uri, mode="create", data_storage_version="2.2", enable_stable_row_ids=True)
        return {"version": created.version, "rows": created.count_rows(), "created": True}

    ds.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(rows)
    reopened = lance.dataset(to_uri)
    return {"version": reopened.version, "rows": reopened.count_rows(), "created": False}


def run(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Execute one dummy silver hop. Returns the result the mover records as its completion."""
    e = env if env is not None else dict(os.environ)
    from_uri = e.get("FROM_URI", "")
    to_uri = e.get("TO_URI", "")
    if not from_uri or not to_uri:
        raise ValueError("FROM_URI and TO_URI are required")

    raw_base = e.get("BASE_VERSION", "").strip()
    base_version = int(raw_base) if raw_base else None
    run_id = e.get("RUN_ID", "")

    delta = read_delta(from_uri, base_version)
    if delta.num_rows == 0:
        # An empty delta is a legitimate no-op, NOT a failure: a redelivered event whose rows were
        # already processed lands here, and writing an empty version would fire a publication event
        # for data nobody added.
        log.info("dummy transform: empty delta, nothing to do")
        return {"rows_in": 0, "rows_written": 0, "version": None, "skipped": True}

    silver = transform_batch(delta)
    result = write_silver(to_uri, silver, run_id)
    log.info("dummy transform wrote %s rows -> version %s (run %s)", silver.num_rows, result["version"], run_id)
    return {
        "rows_in": delta.num_rows,
        "rows_written": silver.num_rows,
        "version": result["version"],
        "run_id": run_id,
        "schema": [f.name for f in SILVER_SCHEMA],
        "skipped": False,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(run()))
    return 0
