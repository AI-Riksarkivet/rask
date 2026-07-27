"""Build .cache/batches.db from the master CSV + manifest counts.

Inputs (paths relative to rask repo root):
  - The master CSV listing the 1633 batches we plan to HTR.
    Default: $RASK_BATCH_MASTER_CSV or /home/morgan/!_output/Serier och volymer att HTRa 2026- - Prel lista batchar 2026.csv
  - .cache/manifest-counts.csv produced by scripts/count_manifests.py
    (run that first to refresh page_count + manifest_status).

Output:
  - .cache/batches.db (SQLite). Idempotent: dropped + rebuilt each run.

Schema mirrors the previous ra-atr version but is the canonical home now.
The default IIIF endpoint is `iiifintern` because we confirmed it covers
all 1633 batches (lbiiif has ~242 access-restricted volumes).
"""

import csv
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
DEFAULT_CSV = "/home/morgan/!_output/Serier och volymer att HTRa 2026- - Prel lista batchar 2026.csv"
MANIFEST_CSV = REPO / ".cache" / "manifest-counts.csv"
DB_PATH = REPO / ".cache" / "batches.db"

DEFAULT_ENDPOINT = "iiifintern"  # https://iiifintern-ai.ra.se — full coverage of our list

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    arkiv_referenskod TEXT,
    arkiv_titel TEXT,
    volym TEXT,
    rattighetsmarkning_volym TEXT,
    rattighetsmarkning_batch TEXT,
    startdatum TEXT,
    slutdatum TEXT,
    htrad_tidigare TEXT,

    -- manifest discovery
    page_count INTEGER,                         -- expected pages (from IIIF manifest)
    iiif_endpoint TEXT,                         -- 'iiifintern' | 'lbiiif' | 'unknown'
    manifest_status TEXT,                       -- 'ok'|'http_403'|'http_400'|'error'|'pending'
    manifest_error TEXT,
    fetched_at TEXT,

    -- HTR progress (reconciled from S3 by sync_from_s3.py)
    cached_pages INTEGER DEFAULT 0,             -- jpgs in images-batch/<batch>/
    transcribed_pages INTEGER DEFAULT 0,        -- xmls in images-batch-alto/<batch>/
    htr_status TEXT DEFAULT 'pending',          -- pending|cached|partial|done|verification_failed
    started_at TEXT,
    finished_at TEXT,
    last_error TEXT,
    last_synced_at TEXT,

    -- chunk assignment (set by scripts/chunk_batches.py with LPT bin packing)
    chunk_id INTEGER,                           -- 0..chunk_total-1, or NULL if not chunked
    chunk_total INTEGER,                        -- total number of chunks at assignment time

    -- Ray submission state (set by viewer's submission service when a chunk
    -- is submitted via POST /api/v1/chunks/{id}/submit or the orchestrator loop)
    current_rayjob_id TEXT,                     -- last submitted submission_id (audit trail)
    current_rayjob_submitted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_manifest_status ON batches(manifest_status);
CREATE INDEX IF NOT EXISTS idx_endpoint ON batches(iiif_endpoint);
CREATE INDEX IF NOT EXISTS idx_pages ON batches(page_count);
CREATE INDEX IF NOT EXISTS idx_htr_status ON batches(htr_status);
CREATE INDEX IF NOT EXISTS idx_chunk ON batches(chunk_id);
"""


def load_master_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_manifest_csv(path: Path) -> dict[str, tuple[int | None, str | None]]:
    if not path.exists():
        return {}
    out: dict[str, tuple[int | None, str | None]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            bid = row["batch_id"]
            pages = int(row["pages"]) if row["pages"] else None
            err = row["error"] or None
            out[bid] = (pages, err)
    return out


def classify(pages: int | None, error: str | None) -> tuple[str, str | None]:
    """Return (manifest_status, manifest_error)."""
    if pages is not None and error is None:
        return "ok", None
    if error == "HTTP 403":
        return "http_403", error
    if error == "HTTP 400":
        return "http_400", error
    if error:
        return "error", error
    return "pending", None


def main() -> None:
    csv_path = Path(os.environ.get("RASK_BATCH_MASTER_CSV", DEFAULT_CSV))
    if not csv_path.exists():
        raise SystemExit(f"missing master CSV: {csv_path}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    rows = load_master_csv(csv_path)
    manifest = load_manifest_csv(MANIFEST_CSV)
    now = datetime.now(UTC).isoformat(timespec="seconds")

    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    inserted = 0
    for row in rows:
        bid = row["BatchID"].strip()
        if not bid:
            continue
        pages, err = manifest.get(bid, (None, None))
        status, err_str = classify(pages, err)
        con.execute(
            """INSERT OR REPLACE INTO batches(
                batch_id, arkiv_referenskod, arkiv_titel, volym,
                rattighetsmarkning_volym, rattighetsmarkning_batch,
                startdatum, slutdatum, htrad_tidigare,
                page_count, iiif_endpoint, manifest_status, manifest_error, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                bid,
                row["Arkivets referenskod"].strip() or None,
                row["Arkivets titel"].strip() or None,
                row["Volym"].strip() or None,
                row["Rättighetsmärkning (volym)"].strip() or None,
                row["Rättighetsmärkning (batch)"].strip() or None,
                row["Startdatum"].strip() or None,
                row["Slutdatum"].strip() or None,
                row["HTRad batch sedan tidigare"].strip() or None,
                pages,
                DEFAULT_ENDPOINT,
                status,
                err_str,
                now if status != "pending" else None,
            ),
        )
        inserted += 1

    con.commit()

    print(f"Inserted {inserted} rows into {DB_PATH}")
    print(f"DB size: {DB_PATH.stat().st_size:,} bytes\n")
    print(f"{'manifest_status':<18}{'count':>8}{'total_pages':>14}")
    for status, n, p in con.execute("SELECT manifest_status, COUNT(*), COALESCE(SUM(page_count), 0) FROM batches GROUP BY 1 ORDER BY 2 DESC"):
        print(f"{status:<18}{n:>8}{p:>14,}")
    pmin, pmax, pavg = next(con.execute("SELECT MIN(page_count), MAX(page_count), AVG(page_count) FROM batches WHERE page_count IS NOT NULL"))
    if pmin is not None:
        print(f"\nPage-count range: {pmin}..{pmax} (mean {pavg:.0f})")
    con.close()


if __name__ == "__main__":
    main()
