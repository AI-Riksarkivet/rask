"""Tick-driven coordinator for sync + prefetch + htr.

Three slots, all running independently per tick:

  - SYNC: run scripts/sync_from_s3.py to refresh batches.db from S3
  - PREFETCH: if no prefetch-* job is RUNNING/PENDING, pick the lowest
    chunk where cached_pages < page_count and submit
    `--pipeline prefetch` for it. Max 1 in flight.
  - HTR: if no htr-* job is RUNNING/PENDING, pick the lowest chunk where
    transcribed_pages < page_count AND cached_pages >= 95% of
    page_count (i.e. prefetch is done enough), and submit `--pipeline
    htr`. Max 1 in flight.

The two slots run in parallel — net effect is HTR for chunk N happens
alongside prefetch for chunk N+1.

Run modes:

    uv run python scripts/orchestrator.py tick
        one-shot — does sync + prefetch_next + htr_next, exits.
        Suitable for cron (`* * * * *` is fine; sync is idempotent).

    uv run python scripts/orchestrator.py loop --interval 60
        long-running — runs `tick` every <interval> seconds. Run under
        tmux/screen/systemd. Ctrl-C to stop.

    uv run python scripts/orchestrator.py orchestrate
        same as `tick` but skips the sync step. Useful when you want to
        run sync at a slower cadence than the orchestration ticks.

The script sources REPO/.env at startup so AWS_*/HCP_*/IIIF_* env vars
are available regardless of how it's launched.
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv


REPO = Path(__file__).resolve().parents[2]
DB_PATH = REPO / ".cache" / "batches.db"
SUBMIT_SCRIPT = REPO / "components" / "scripts" / "submit_chunks.py"
SYNC_SCRIPT = REPO / "components" / "scripts" / "sync_from_s3.py"
DASHBOARD_URL = os.environ.get("RAY_DASHBOARD_URL", "http://localhost:8265")
HTR_READY_FRACTION = 0.95  # treat a chunk as "prefetched enough" once 95% of pages are cached
FAIL_COOLDOWN_SECS = 600  # don't auto-resubmit a chunk whose previous run FAILED less than this ago


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def _fetch_jobs() -> list[dict] | None:
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{DASHBOARD_URL}/api/jobs/")
            r.raise_for_status()
            return r.json()
    except Exception as exc:
        _log(f"  ray dashboard unreachable ({DASHBOARD_URL}): {exc}")
        return None


def _job_for(jobs: list[dict], submission_id: str) -> dict | None:
    for j in jobs:
        if j.get("submission_id") == submission_id:
            return j
    return None


def _running_for(jobs: list[dict], prefix: str) -> dict | None:
    for j in jobs:
        sid = j.get("submission_id") or ""
        if sid.startswith(prefix) and j.get("status") in ("RUNNING", "PENDING"):
            return j
    return None


def _delete_job(submission_id: str) -> None:
    """Best-effort delete of a Ray job submission_id (terminal states only)."""
    from ray.job_submission import JobSubmissionClient

    try:
        c = JobSubmissionClient(DASHBOARD_URL)
        c.delete_job(submission_id)
        _log(f"  deleted prior {submission_id}")
    except Exception as exc:
        _log(f"  could not delete {submission_id}: {exc}")


def _recently_failed(job: dict | None) -> bool:
    """True if ``job`` is FAILED and its end_time was less than FAIL_COOLDOWN_SECS ago.

    Ray's end_time is ms-since-epoch. A FAILED chunk gets a cooldown so we
    don't hot-loop on a permabad page; STOPPED (user-cancelled) and
    SUCCEEDED don't trigger this guard.
    """
    if not job or job.get("status") != "FAILED":
        return False
    end = job.get("end_time")
    if not end:
        return False
    return (time.time() * 1000 - float(end)) < FAIL_COOLDOWN_SECS * 1000


def _chunks_needing_prefetch(db: sqlite3.Connection) -> list[int]:
    """Chunks where any batch has cached_pages < page_count.

    Returned in random order so a chunk whose runner exits "nothing to do"
    in <1s (e.g. manifest reports more pages than IIIF actually serves)
    does not stay glued to the head of the queue and starve later chunks.
    """
    rows = db.execute(
        """SELECT chunk_id FROM batches
           WHERE chunk_id IS NOT NULL
             AND manifest_status = 'ok'
             AND COALESCE(cached_pages, 0) < COALESCE(page_count, 0)
           GROUP BY chunk_id"""
    ).fetchall()
    out = [r[0] for r in rows]
    random.shuffle(out)
    return out


def _chunks_needing_htr(db: sqlite3.Connection) -> list[int]:
    """Chunks where transcribed < expected AND cached covers >= HTR_READY_FRACTION.

    Returned in random order — see _chunks_needing_prefetch for why.
    """
    rows = db.execute(
        """SELECT chunk_id,
                  COALESCE(SUM(page_count), 0)        AS expected,
                  COALESCE(SUM(cached_pages), 0)      AS cached,
                  COALESCE(SUM(transcribed_pages), 0) AS transcribed
             FROM batches
            WHERE chunk_id IS NOT NULL AND manifest_status = 'ok'
            GROUP BY chunk_id"""
    ).fetchall()
    out = []
    for cid, expected, cached, transcribed in rows:
        if not expected:
            continue
        if transcribed >= expected:
            continue
        if cached / expected >= HTR_READY_FRACTION:
            out.append(cid)
    random.shuffle(out)
    return out


def _submission_id(chunk_id: int, pipeline: str) -> str:
    """Mirror scripts/submit_chunks.py:chunk_name(). Hard-coded chunk_total=100."""
    prefix = "htr" if pipeline == "htr" else pipeline
    return f"{prefix}-chunk-{chunk_id:03d}-of-100"


def _submit(chunk_id: int, pipeline: str) -> None:
    cmd = [
        "uv",
        "run",
        "python",
        str(SUBMIT_SCRIPT),
        "--mode",
        "local",
        "--pipeline",
        pipeline,
        "--chunk",
        str(chunk_id),
    ]
    _log(f"  submit {pipeline} chunk={chunk_id}")
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, check=False)  # noqa: S603 — args are local literals
    if r.returncode != 0:
        _log(f"  !! submit failed (rc={r.returncode}): {r.stderr.strip()[-300:]}")
    else:
        last = (r.stdout.strip().splitlines() or [""])[-1]
        _log(f"  ok: {last}")


def _do_sync() -> None:
    _log("sync_from_s3...")
    cmd = ["uv", "run", "python", str(SYNC_SCRIPT)]
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, check=False)  # noqa: S603 — args are local literals
    if r.returncode != 0:
        _log(f"  !! sync failed (rc={r.returncode}): {r.stderr.strip()[-300:]}")
    else:
        # Last non-empty line is usually the headline progress percent.
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        for ln in lines[-3:]:
            _log(f"  {ln.strip()}")


def _do_orchestrate() -> None:
    jobs = _fetch_jobs()
    if jobs is None:
        return

    db = sqlite3.connect(DB_PATH)
    try:
        # Prefetch slot
        running = _running_for(jobs, "prefetch-")
        if running:
            _log(f"prefetch slot: BUSY ({running['submission_id']} {running['status']})")
        else:
            for cid in _chunks_needing_prefetch(db):
                sid = _submission_id(cid, "prefetch")
                prior = _job_for(jobs, sid)
                if _recently_failed(prior):
                    _log(f"prefetch slot: skipping chunk {cid} — recent FAILED ({sid}); cooldown {FAIL_COOLDOWN_SECS}s")
                    continue
                # Free the slot if a previous terminal-state run is still
                # holding the submission_id; Ray rejects duplicate IDs even
                # for SUCCEEDED/STOPPED jobs.
                if prior:
                    _delete_job(sid)
                _submit(cid, "prefetch")
                break
            else:
                _log("prefetch slot: idle, nothing eligible")

        # HTR slot — re-fetch jobs in case the prefetch submit changed state.
        jobs = _fetch_jobs() or jobs
        running = _running_for(jobs, "htr-")
        if os.environ.get("RASK_ORCHESTRATOR_SKIP_HTR") == "1":
            # Operator-controlled pause: we still log so it's obvious in the
            # tick log that HTR is intentionally idle, not stuck.
            _log("htr slot:      PAUSED (RASK_ORCHESTRATOR_SKIP_HTR=1)")
        elif running:
            _log(f"htr slot:      BUSY ({running['submission_id']} {running['status']})")
        else:
            for cid in _chunks_needing_htr(db):
                sid = _submission_id(cid, "htr")
                prior = _job_for(jobs, sid)
                if _recently_failed(prior):
                    _log(f"htr slot:      skipping chunk {cid} — recent FAILED ({sid}); cooldown {FAIL_COOLDOWN_SECS}s")
                    continue
                if prior:
                    _delete_job(sid)
                _submit(cid, "htr")
                break
            else:
                _log("htr slot:      idle, nothing eligible")
    finally:
        db.close()


def _tick(skip_sync: bool = False) -> None:
    _log("---- tick ----")
    if not skip_sync:
        _do_sync()
    _do_orchestrate()


def main() -> None:
    load_dotenv(REPO / ".env")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="one-shot: sync + orchestrate, exit (cron-friendly)")
    sub.add_parser("orchestrate", help="one-shot: orchestrate only (no sync)")
    sub.add_parser("sync", help="one-shot: sync only")
    p_loop = sub.add_parser("loop", help="long-running: tick every --interval seconds")
    p_loop.add_argument("--interval", type=int, default=60, help="seconds between ticks (default 60)")
    p_loop.add_argument("--no-sync", action="store_true", help="skip sync step in each tick")
    args = ap.parse_args()

    if args.cmd == "tick":
        _tick()
    elif args.cmd == "orchestrate":
        _tick(skip_sync=True)
    elif args.cmd == "sync":
        _do_sync()
    elif args.cmd == "loop":
        try:
            while True:
                try:
                    _tick(skip_sync=args.no_sync)
                except Exception as exc:
                    _log(f"  tick raised: {exc!r}")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            _log("interrupted, exiting")
            sys.exit(0)


if __name__ == "__main__":
    main()
