"""Build the topic hierarchy as a JSONB value in Lance (for the Tree treemap).

The per-chunk ``topic_l0…`` columns drive *filtering*; this module derives the
nested *hierarchy* (broadest layer → … → finest, with chunk counts) and stores it
as a single-row ``topics`` Lance table whose ``hierarchy`` column is Lance JSONB
(``pa.json_()`` — the same extension type used for ``alignments_json``). No sidecar
file: the tree lives in the dataset, the backend reads one row, the LayerChart
``<Treemap>`` renders it.

Pure Lance + stdlib (no Toponymy), so it runs in the normal ratch env — called by
``feature topics`` after the isolated worker writes the columns, and runnable
standalone to backfill an existing build:

    uv run python -c "from ratch.features.topic_tree import build_topic_tree; \\
        build_topic_tree('transcripts_v2.lance')"
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

# Noise rows (cluster -1) are stored as NULL; surface them as one labelled bucket
# so every path reaches full depth and only true leaves carry a value (d3 sums
# leaf values up — a branch that also held a value would be double-counted).
# Public + surfaced via /api/topics so the frontend never re-hardcodes it.
NOISE_LABEL = "(övrigt)"


def topic_layer_columns(schema_names: list[str]) -> list[str]:
    """``topic_l0 … topic_l{N-1}`` present on the table, finest-first.

    The single source of truth for "which columns are topic layers", shared by
    the tree builder, the search topic filter, and the atlas colour channel so
    they can't drift from each other.
    """
    return sorted(
        (c for c in schema_names if c.startswith("topic_l")),
        key=lambda c: int(c.removeprefix("topic_l")),
    )


def _nest(rows: list[dict[str, Any]], broad_to_fine: list[str]) -> dict[str, Any]:
    """Fold per-chunk layer labels into a ``{name, children|value}`` tree.

    Each chunk contributes one root→leaf path (broadest → finest); the leaf's
    ``value`` is the chunk count for that exact path. Branch nodes carry no value
    (d3 sums leaf values up), so only leaves get ``value`` — the shape a treemap wants.
    """
    counts: Counter[tuple[str, ...]] = Counter()
    for row in rows:
        # Explicit None test (not falsy) so only true NULLs — HDBSCAN noise —
        # fall into the bucket; a real (if empty) label stays its own path.
        counts[tuple(NOISE_LABEL if row[col] is None else row[col] for col in broad_to_fine)] += 1
    return _subtree("Alla ämnen", counts)


def _subtree(name: str, counts: dict[tuple[str, ...], int]) -> dict[str, Any]:
    """Recursively split path→count by the head element into a treemap node.

    A leaf (all paths consumed) carries the chunk ``value``; a branch carries
    ``children`` (sorted largest-first). Only leaves get ``value`` so d3 sums cleanly.
    """
    if all(len(path) == 0 for path in counts):
        return {"name": name, "value": int(sum(counts.values()))}
    groups: dict[str, Counter[tuple[str, ...]]] = defaultdict(Counter)
    for path, count in counts.items():
        groups[path[0]][path[1:]] += count
    kids = [_subtree(head, group) for head, group in groups.items()]
    kids.sort(key=_node_size, reverse=True)
    return {"name": name, "children": kids}


def _node_size(node: dict[str, Any]) -> int:
    """Total chunk count under a finalized node (leaf value or sum of children)."""
    if "value" in node:
        return int(node["value"])
    return sum(_node_size(child) for child in node["children"])


def build_topic_tree(db_path: str | Path) -> int:
    """Write/refresh the ``topics`` table (one row: ``hierarchy`` JSONB + ``layers``).

    Returns the number of chunks folded into the tree. Raises if no ``topic_l*``
    columns exist yet (run ``ratch feature topics`` first).
    """
    import lance
    import pyarrow as pa

    from ratch.core.dataset import overwrite_dataset

    db_path = Path(db_path)
    chunks = lance.dataset(str(db_path / "chunks.lance"))
    layer_cols = topic_layer_columns(chunks.schema.names)
    if not layer_cols:
        raise ValueError(f"no topic_l* columns on {db_path}/chunks.lance — run `ratch feature topics` first.")

    rows = chunks.to_table(columns=layer_cols).to_pylist()
    broad_to_fine = list(reversed(layer_cols))  # topic_l{N-1} (broad) → topic_l0 (fine)
    tree = _nest(rows, broad_to_fine)
    n_layers = len(layer_cols)

    table = pa.table(
        {
            "hierarchy": pa.array([json.dumps(tree, ensure_ascii=False)], type=pa.json_()),
            "layers": pa.array([n_layers], pa.int32()),
            "n_chunks": pa.array([len(rows)], pa.int64()),
        }
    )
    topics_path = db_path / "topics.lance"
    overwrite_dataset(topics_path, table)
    logger.info("wrote topic tree: %s chunks · %s layers → %s", len(rows), n_layers, topics_path)
    return len(rows)
