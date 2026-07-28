"""Single-column cosine vector search over the row table.

Ported from ``backend.search.vector``; the hit projection now arrives from the
descriptor-derived target instead of a module constant. A failed search raises
a domain :class:`ValidationError` (HTTP 400, stable message) with the real
Lance error logged, never interpolated.
"""

from __future__ import annotations

import logging
from typing import Any

from search.services.constants import (
    VECTOR_MAX_NPROBES,
    VECTOR_NPROBES,
    VECTOR_REFINE_FACTOR,
)
from service_kit.exceptions import ValidationError


logger = logging.getLogger(__name__)


def vector_search(
    table: Any,
    vec: Any,
    column: str,
    *,
    payload_columns: list[str],
    n: int,
    where: str | None,
    prefilter: bool = True,
) -> list[dict[str, Any]]:
    """Run a cosine vector search on ``column``; returns raw list of dicts.

    Returns ``[]`` when the embedding column doesn't exist (a stale descriptor
    or a leg the pipeline hasn't built), so fusion modes degrade to the other
    rankings instead of erroring. ``vector_column_name`` is always explicit —
    multi-vector tables make Lance's auto-pick ambiguous.
    """
    if vec is None or column not in table.schema.names:
        return []
    try:
        qb = (
            table.search(vec.tolist(), vector_column_name=column)
            .distance_type("cosine")
            .minimum_nprobes(VECTOR_NPROBES)
            .maximum_nprobes(VECTOR_MAX_NPROBES)
            .refine_factor(VECTOR_REFINE_FACTOR)
            .select([*payload_columns, "_distance"])
            .limit(n)
        )
        if where:
            qb = qb.where(where, prefilter=prefilter)
        return qb.to_list()
    except Exception as e:
        logger.warning("vector search failed", exc_info=True)
        raise ValidationError("vector search failed") from e
