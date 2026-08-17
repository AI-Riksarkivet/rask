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

from dummy_runner.lineage import build_run_event, emit
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
    # PROVENANCE IDENTITY, separate from the URIs. `to_id`/`from_id` are the CATALOG identifiers the
    # lineage graph and the FGA objects are keyed by (`silver$dummy`), which a storage URI is not —
    # emitting the URI would name a node no grant matches, hiding the run from every recipient.
    # Defaulted from the URI's stem only so a lane that has not wired them still produces a
    # well-formed graph rather than crashing on provenance.
    to_id = e.get("TO_ID", "") or _identifier_from(to_uri)
    from_id = e.get("FROM_ID", "") or _identifier_from(from_uri)
    originator = e.get("ORIGINATOR", "")
    project = e.get("PROJECT", "")

    def _emit(event_type: str, **over: object) -> None:
        # Best effort, and deliberately AFTER the work: provenance must never fail a run that
        # actually produced data. A redelivery reuses `run_id`, so the notification id
        # (`runId@STATE`) dedupes rather than putting a second row in anyone's inbox.
        emit(build_run_event(event_type=event_type, run_id=run_id, to_id=to_id, from_id=from_id, originator=originator, project=project, **over))

    try:
        delta = read_delta(from_uri, base_version)
        if delta.num_rows == 0:
            # An empty delta is a legitimate no-op, NOT a failure: a redelivered event whose rows were
            # already processed lands here, and writing an empty version would fire a publication event
            # for data nobody added. It is still a COMPLETED run and is emitted as one — a terminal
            # event missing from the graph is what makes "did my trigger do anything?" unanswerable.
            log.info("dummy transform: empty delta, nothing to do")
            _emit("COMPLETE", rows=0)
            return {"rows_in": 0, "rows_written": 0, "version": None, "skipped": True}

        silver = transform_batch(delta)
        result = write_silver(to_uri, silver, run_id)
    except Exception as exc:
        # A FAIL carries no version, because the run committed nothing. Re-raised so the job still
        # exits non-zero — the event records what happened, it does not absolve the failure.
        _emit("FAIL", error=str(exc))
        raise

    log.info("dummy transform wrote %s rows -> version %s (run %s)", silver.num_rows, result["version"], run_id)
    _emit("COMPLETE", rows=silver.num_rows, version=result["version"])
    return {
        "rows_in": delta.num_rows,
        "rows_written": silver.num_rows,
        "version": result["version"],
        "run_id": run_id,
        "schema": [f.name for f in SILVER_SCHEMA],
        "skipped": False,
    }


def _identifier_from(uri: str) -> str:
    """A best-effort catalog identifier from a dataset URI — the stem, minus `.lance`.

    Only a fallback for a lane that has not wired `TO_ID`/`FROM_ID`. It cannot recover the namespace
    a real identifier carries, so a deployment that relies on it produces graph nodes that will not
    match tenant-qualified grants. Wire the ids.
    """
    return uri.rstrip("/").rsplit("/", 1)[-1].removesuffix(".lance")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(run()))
    return 0
