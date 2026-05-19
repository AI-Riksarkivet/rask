"""Assign each batch a chunk_id (0..N-1) using LPT bin packing.

Longest-processing-time-first (LPT): sort batches by page_count descending,
greedily place each into the chunk with the smallest current total. Optimal
within (4/3 - 1/(3N)) of the makespan-minimizing partition for our shape.

Idempotent — same `--num` produces the same assignment because (a) we sort
by (page_count desc, batch_id asc) for stable ties and (b) the heap
deterministically picks the first chunk with the smallest total.

Usage:
    uv run python scripts/chunk_batches.py --num 100
"""

import argparse
import heapq
import sqlite3
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DB_PATH = REPO / ".cache" / "batches.db"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--num", type=int, required=True, help="Number of chunks (e.g. 100)")
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument(
        "--include",
        choices=["all", "ok"],
        default="ok",
        help="Which batches to chunk: 'ok' (default; only manifest_status=ok) or 'all'",
    )
    args = parser.parse_args()

    if args.num < 1:
        raise SystemExit("--num must be >= 1")

    db = sqlite3.connect(args.db)
    if args.include == "ok":
        rows = db.execute("SELECT batch_id, COALESCE(page_count, 0) AS pages FROM batches WHERE manifest_status = 'ok'").fetchall()
    else:
        rows = db.execute("SELECT batch_id, COALESCE(page_count, 0) AS pages FROM batches").fetchall()
    if not rows:
        raise SystemExit("no batches to chunk")

    # Sort by page_count descending, batch_id ascending — stable + deterministic.
    rows.sort(key=lambda r: (-r[1], r[0]))

    # Min-heap keyed on (current_total, chunk_id) so the "smallest" chunk pops first.
    # Tie-break on chunk_id keeps assignments deterministic across runs.
    heap: list[tuple[int, int]] = [(0, i) for i in range(args.num)]
    heapq.heapify(heap)

    assignments: list[tuple[int, int, str]] = []  # (chunk_id, chunk_total, batch_id)
    chunk_totals = [0] * args.num
    chunk_counts = [0] * args.num
    for batch_id, pages in rows:
        total, cid = heapq.heappop(heap)
        assignments.append((cid, args.num, batch_id))
        chunk_totals[cid] = total + pages
        chunk_counts[cid] += 1
        heapq.heappush(heap, (total + pages, cid))

    # Reset chunk_id for everything (so excluded rows are NULL again), then write.
    db.execute("UPDATE batches SET chunk_id = NULL, chunk_total = NULL")
    db.executemany(
        "UPDATE batches SET chunk_id = ?, chunk_total = ? WHERE batch_id = ?",
        assignments,
    )
    db.commit()

    pmin, pmax = min(chunk_totals), max(chunk_totals)
    cmin, cmax = min(chunk_counts), max(chunk_counts)
    mean_pages = sum(chunk_totals) / args.num
    spread = (pmax - pmin) / mean_pages * 100 if mean_pages else 0
    print(f"Assigned {len(rows)} batches into {args.num} chunks ({args.include}):")
    print(f"  pages per chunk:    min={pmin:,}  max={pmax:,}  mean={mean_pages:,.0f}  spread={spread:.2f}%")
    print(f"  batches per chunk:  min={cmin}    max={cmax}")
    print()
    print(f"  {'chunk':>6}{'batches':>10}{'pages':>14}")
    if args.num > 10:
        show: list[int | None] = [*range(5), None, *range(max(args.num - 5, 5), args.num)]
    else:
        show = list(range(args.num))
    for i in show:
        if i is None:
            print(f"  {'...':>6}{'...':>10}{'...':>14}")
        else:
            print(f"  {i:>6}{chunk_counts[i]:>10}{chunk_totals[i]:>14,}")
    db.close()


if __name__ == "__main__":
    main()
