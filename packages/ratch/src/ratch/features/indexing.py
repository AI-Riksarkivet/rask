"""Distributed index builds (P1.6): the standard index set via lance-ray.

Plans the same indexes the old per-command builds produced, with the same
gates the engine enforced (a partial-NULL column or a table smaller than
``num_partitions`` crashes/degrades the trainer — those plans surface as
``blocked`` with the reason instead of running). IVF_PQ builds run distributed
via ``lance_ray.create_index``; FTS/BTREE via ``lance_ray.create_scalar_index``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import lance
from pydantic import BaseModel


logger = logging.getLogger(__name__)

#: The corpus FTS contract: Swedish stemmer, phrase-capable, stop words kept.
FTS_LANGUAGE = "Swedish"


#: lance-ray's distributed IVF trainer needs ``num_partitions * sample_rate``
#: rows (hard error below); the local trainer only needs ``rows >= partitions``.
DISTRIBUTED_SAMPLE_RATE = 256


class IndexPlan(BaseModel):
    table: str
    column: str
    kind: Literal["IVF_PQ", "FTS", "BTREE"]
    num_partitions: int = 0
    num_sub_vectors: int = 0
    distributed: bool = True
    blocked: str | None = None

    def __str__(self) -> str:
        params = f" cosine {self.num_partitions}/{self.num_sub_vectors}" if self.kind == "IVF_PQ" else ""
        state = f"BLOCKED: {self.blocked}" if self.blocked else ("ready" if self.distributed else "ready (local build)")
        return f"{self.table}.{self.column}  {self.kind}{params}  [{state}]"


_IndexKind = Literal["IVF_PQ", "FTS", "BTREE"]

#: (table, column, kind, num_partitions, num_sub_vectors) — the standard set.
_STANDARD: tuple[tuple[str, str, _IndexKind, int, int], ...] = (
    ("chunks", "text_embedding", "IVF_PQ", 256, 64),
    ("chunks", "text", "FTS", 0, 0),
    ("chunks", "doc_id", "BTREE", 0, 0),
    ("chunks", "audio_path", "BTREE", 0, 0),
    ("chunk_frames", "frame_embedding", "IVF_PQ", 256, 64),
    ("chunk_frames", "caption_embedding", "IVF_PQ", 256, 64),
    ("chunk_frames", "caption", "FTS", 0, 0),
    ("speaker_embeddings", "embedding", "IVF_PQ", 256, 16),
)


def plan_indexes(db_path: str | Path) -> list[IndexPlan]:
    """The standard index ops for ``db_path``, each gated exactly like the engine."""
    plans: list[IndexPlan] = []
    for table, column, kind, partitions, sub_vectors in _STANDARD:
        uri = Path(db_path) / f"{table}.lance"
        if not uri.exists():
            continue
        ds = lance.dataset(str(uri))
        plan = IndexPlan(
            table=table,
            column=column,
            kind=kind,
            num_partitions=partitions,
            num_sub_vectors=sub_vectors,
        )
        if column not in ds.schema.names:
            plan = plan.model_copy(update={"blocked": "column missing"})
        elif kind == "IVF_PQ":
            nulls = ds.count_rows(filter=f"{column} IS NULL")
            rows = ds.count_rows()
            if nulls > 0:
                plan = plan.model_copy(update={"blocked": f"{nulls} NULL row(s)"})
            elif rows < partitions:
                plan = plan.model_copy(update={"blocked": f"{rows} row(s) < num_partitions={partitions} (flat search until then)"})
            elif rows < partitions * DISTRIBUTED_SAMPLE_RATE:
                plan = plan.model_copy(update={"distributed": False})
        plans.append(plan)
    return plans


def run_indexes(db_path: str | Path, plans: list[IndexPlan], *, num_workers: int = 4) -> int:
    """Build every ready plan distributed. Returns the number built."""
    import lance_ray

    built = 0
    for plan in plans:
        if plan.blocked:
            logger.info("skipping %s", plan)
            continue
        # Resolved: Ray index workers can't see driver-relative paths.
        uri = str((Path(db_path) / f"{plan.table}.lance").resolve())
        logger.info("building %s", plan)
        if plan.kind == "IVF_PQ":
            if plan.distributed:
                lance_ray.create_index(
                    uri,
                    column=plan.column,
                    index_type="IVF_PQ",
                    metric="cosine",
                    num_partitions=plan.num_partitions,
                    num_sub_vectors=plan.num_sub_vectors,
                    num_workers=num_workers,
                )
            else:
                # Below the distributed trainer's rows >= partitions*sample_rate
                # floor — the local trainer handles small tables fine.
                import lancedb

                from ratch.core.engine import ensure_vector_index

                table = lancedb.connect(str(Path(uri).parent)).open_table(plan.table)
                ensure_vector_index(
                    table,
                    plan.column,
                    num_partitions=plan.num_partitions,
                    num_sub_vectors=plan.num_sub_vectors,
                )
        elif plan.kind == "FTS":
            lance_ray.create_scalar_index(
                uri,
                column=plan.column,
                index_type="INVERTED",
                num_workers=num_workers,
                language=FTS_LANGUAGE,
                with_position=True,
                remove_stop_words=False,
            )
        else:
            lance_ray.create_scalar_index(uri, column=plan.column, index_type="BTREE", num_workers=num_workers)
        built += 1
    return built
