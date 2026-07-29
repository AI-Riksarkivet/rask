"""Step 1/3 of the knowledge-graph build: export chunks → JSONL.

Runs in the PROJECT venv (uses pylance). Reads ``chunks.lance`` and writes one
JSON object per chunk to a JSONL the isolated LightRAG build (step 2) consumes.
Each chunk's stable key ``doc_id:speech_id:chunk_id`` becomes its LightRAG
``file_path`` so every extracted entity/edge carries it back to a playable clip.

    uv run python scripts/kg/export_chunks.py --db transcripts_v2.lance \
        --out kg_work/chunks.jsonl

``--max-per-doc`` / ``--limit`` scope the export (0 = no cap) for a smaller run.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import lance


_COLUMNS = ["doc_id", "speech_id", "chunk_id", "namn", "start", "end", "text"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Export chunks → JSONL for the KG build.")
    parser.add_argument("--db", default="transcripts_v2.lance")
    parser.add_argument("--out", default="kg_work/chunks.jsonl")
    parser.add_argument("--max-per-doc", type=int, default=0, help="cap chunks per video (0 = all)")
    parser.add_argument("--limit", type=int, default=0, help="cap total chunks (0 = all)")
    args = parser.parse_args()

    rows = lance.dataset(f"{args.db}/chunks.lance").to_table(columns=_COLUMNS).to_pylist()
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_doc[row["doc_id"]].append(row)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written, skipped_empty = 0, 0
    with out_path.open("w", encoding="utf-8") as fh:
        for crs in by_doc.values():
            crs.sort(key=lambda r: (int(r["speech_id"]), int(r["chunk_id"])))
            for row in crs[: args.max_per_doc] if args.max_per_doc else crs:
                if args.limit and written >= args.limit:
                    break
                # Empty-text chunks have nothing to extract and make LightRAG
                # raise "Set of Tasks/Futures is empty" (the doc is marked FAILED
                # and skipped). Drop them at the source so they never enter the
                # build and never burn an LLM call.
                text = (row["text"] or "").strip()
                if not text:
                    skipped_empty += 1
                    continue
                fh.write(
                    json.dumps(
                        {
                            "doc_id": row["doc_id"],
                            "speech_id": int(row["speech_id"]),
                            "chunk_id": int(row["chunk_id"]),
                            "namn": row["namn"] or row["doc_id"],
                            "start": float(row["start"]),
                            "end": float(row["end"]),
                            "text": text,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                written += 1
            if args.limit and written >= args.limit:
                break

    print(f"wrote {written} chunks from {len(by_doc)} docs -> {out_path} (skipped {skipped_empty} empty-text chunks)")


if __name__ == "__main__":
    main()
