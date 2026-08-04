"""Reciprocal-rank fusion across corpora.

Searching several corpora at once is easy; RANKING the result is not. BM25 is normalised per index
and vector distances depend on the space, so `_score` from corpus A and `_score` from corpus B are
not comparable — sorting a merged list by them produces an ordering that looks authoritative and
means nothing.

RRF is the standard answer and the one chosen here: it throws the scores away and fuses by RANK.

    rrf(d) = Σ  1 / (k + rank_i(d))
             i

Only the ORDER within each list is used, which is the only thing that survives the comparison. A
document that no corpus ranked contributes nothing; one that several rank highly compounds.

`k` damps the top of each list so a single first place cannot dominate a document that several
sources agree on. 60 is the value from the original Cormack et al. paper and the one Lance, Elastic
and Weaviate all default to — worth keeping unless there is a measured reason to move it, because a
tuned constant nobody remembers tuning is worse than a published one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


#: The rank-damping constant. See the module docstring for why 60 and why it is not tuned here.
DEFAULT_K = 60

#: Where the fused score lands on each hit. Named distinctly from `_score` on purpose: a consumer
#: must be able to tell a fused ordering from a single-corpus one, because only the former is
#: comparable across corpora and only the latter carries the engine's own relevance number.
RRF_SCORE = "_rrf"


def _identity(hit: dict[str, Any], key_fields: Sequence[str]) -> tuple[Any, ...]:
    """What makes two hits the SAME document.

    `_dataset` YES, `_table` NO, and the asymmetry is the whole design:

    - Across CORPORA a shared key is coincidence. `doc_id` is namespaced per corpus, so two rows with
      the same id are different things; merging them would drop one and attribute the other to the
      wrong corpus.
    - Across TABLES of ONE corpus it is the same document seen twice. `pages` and `lines` index the
      same material at different granularities, so a document both rank highly is genuine agreement —
      and that agreement is exactly what fusion exists to reward.

    Including `_table` here (the first version of this) makes nothing ever compound, which quietly
    degenerates RRF into rank-interleaving — the option that was explicitly NOT chosen. Two tests
    caught it.
    """
    return (hit.get("_dataset"), *(hit.get(f) for f in key_fields))


def reciprocal_rank_fusion(
    ranked_lists: Iterable[Sequence[dict[str, Any]]],
    *,
    key_fields: Sequence[str],
    k: int = DEFAULT_K,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fuse per-corpus ranked lists into ONE ordering.

    Each input must already be ordered best-first — that ordering is the entire signal. Ties break on
    the identity tuple so the result is deterministic: an unstable order across identical requests
    makes pagination lie, and it is the kind of bug that only shows up under load.
    """
    scored: dict[tuple[Any, ...], float] = {}
    first_seen: dict[tuple[Any, ...], dict[str, Any]] = {}

    for hits in ranked_lists:
        for rank, hit in enumerate(hits):
            identity = _identity(hit, key_fields)
            scored[identity] = scored.get(identity, 0.0) + 1.0 / (k + rank + 1)
            # Keep the FIRST occurrence: within one corpus a document appears once, and across
            # corpora the identity already separates them, so this only matters for a malformed
            # input list — where taking the first is at least deterministic.
            first_seen.setdefault(identity, hit)

    ordered = sorted(scored.items(), key=lambda pair: (-pair[1], tuple(str(x) for x in pair[0])))
    fused = []
    for identity, score in ordered[: limit if limit is not None else len(ordered)]:
        hit = dict(first_seen[identity])
        hit[RRF_SCORE] = score
        fused.append(hit)
    return fused
