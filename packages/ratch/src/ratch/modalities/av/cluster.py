"""Global speaker clustering + eval helpers for ``ratch cluster-speakers``.

Fits :class:`evoc.EVoC` over the per-speaker voiceprints in ``speakers.lance`` and
rewrites the table's ``speaker_cluster`` column. The written assignment is NOT
EVoC's persistence-max ``labels_`` (which at this scale tracks the recording
channel, an order of magnitude coarser than people) but the coarsest
``cluster_layers_`` entry whose within-video false-merge rate stays under
:data:`MAX_SAME_DOC_MERGE_RATE` — see :func:`select_identity_layer`.

The validation helpers (:func:`validate_known_identities`) report PASS/FAIL for a
human-labeled same-person pair (and one suspected pair) so the gate that runs
``cluster-speakers --validate`` can decide whether a FAIL means rejecting the
clustering, loosening ``--min-cluster-size``, or shipping the feature as
experimental.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pyarrow as pa
from pydantic import BaseModel


if TYPE_CHECKING:
    from collections.abc import Callable

    import lancedb

logger = logging.getLogger(__name__)


#: Human-verified same-person pair for ``cluster-speakers --validate``, pinned
#: to explicit (doc_id, speaker_label, tag). Derived from
#: evals/voice_labels_T0001889_c225.json: the query chunk 225 (6187-6216 s of
#: T0001889) overlaps only SPEAKER_13's diarized turns, and every "same"-labeled
#: hit (T0001814 chunks 8/11/47) overlaps only SPEAKER_00's. Deliberately NOT
#: "the doc's largest-total_duration speaker": T0001889 is a 16-speaker panel
#: whose duration-max speaker (SPEAKER_01) is a *different* person — centroid
#: cosine 0.07 to the match, vs 0.79 for SPEAKER_13.
VALIDATION_CONFIRMED_PAIR: Final = (
    ("3df1a0687de89603", "SPEAKER_13", "T0001889"),
    ("6f159850b4e28320", "SPEAKER_00", "T0001814"),
)

#: T0001786's main speaker (largest total_duration — that doc has no chunk-level
#: labels to pin an explicit speaker) is strongly *suspected* (not
#: human-confirmed) to be the same person as the confirmed pair, so it is
#: reported informationally — never as a hard FAIL. Its doc_id is resolved via
#: the chunks table at runtime.
VALIDATION_SUSPECTED_AUDIO: Final = "T0001786_00001.mp4"

#: A cluster layer is identity-usable only while its within-video false-merge
#: rate stays small: two *different* diarized speaker labels in one video are
#: almost surely different people (diarization separated them by voice), so
#: same-doc co-membership inside a cluster measures false merges with no human
#: labels. Diarization over-segmentation makes the metric overcount (reuniting
#: a split speaker is correct but penalized), so it biases toward precision.
MAX_SAME_DOC_MERGE_RATE: Final = 0.05


def doc_id_for_audio_path(db_path: Path, table: str, audio_path: str) -> str | None:
    """First chunks ``doc_id`` whose ``audio_path`` ends with ``audio_path`` (or None)."""
    import lance

    chunks_path = db_path / f"{table}.lance"
    if not chunks_path.exists():
        return None
    # Escape quotes at render time (mirrors service_kit.lancekit.predicate's escaping);
    # % / _ stay live — the suffix wildcard IS the semantics here.
    escaped = audio_path.replace("'", "''")
    rows = lance.dataset(str(chunks_path)).to_table(columns=["doc_id"], filter=f"audio_path LIKE '%{escaped}'", limit=1).to_pylist()
    return rows[0]["doc_id"] if rows else None


def same_doc_merge_rate(labels: np.ndarray, doc_ids: list[str]) -> float:
    """Fraction of clustered members that share a doc with a co-member (0 if none clustered)."""
    members_by_cluster: dict[int, list[int]] = {}
    for i, c in enumerate(labels):
        if c >= 0:
            members_by_cluster.setdefault(int(c), []).append(i)
    clustered = sum(len(m) for m in members_by_cluster.values())
    if clustered == 0:
        return 0.0
    dup = sum(len(m) - len({doc_ids[i] for i in m}) for m in members_by_cluster.values())
    return dup / clustered


def select_identity_layer(clusterer: Any, doc_ids: list[str]) -> tuple[np.ndarray, int, float] | None:
    """(labels, layer index, false-merge rate) of the coarsest identity-usable layer.

    EVoC's own ``labels_`` is the *persistence*-max layer — the dominant density
    scale, which for these voiceprints is the recording channel (measured on the
    real table: 6 clusters, largest 2,667, 45% within-video false merges), an
    order of magnitude coarser than people. For identity we instead take the
    coarsest ``cluster_layers_`` entry (layers run fine → coarse) whose
    within-video false-merge rate stays ≤ :data:`MAX_SAME_DOC_MERGE_RATE` —
    coarser links more cross-video appearances of one person; the bound caps
    demonstrable merges of different people. ``None`` when no layer qualifies.
    """
    import numpy as np

    for li in range(len(clusterer.cluster_layers_) - 1, -1, -1):
        labels = np.asarray(clusterer.cluster_layers_[li], dtype=np.int32)
        rate = same_doc_merge_rate(labels, doc_ids)
        if rate <= MAX_SAME_DOC_MERGE_RATE:
            return labels, li, rate
    return None


def validate_known_identities(
    db_path: Path,
    table: str,
    speakers: pa.Table,
    clusters: np.ndarray,
    *,
    echo: Callable[[str], None],
) -> None:
    """Print PASS/FAIL for the confirmed same-person pair (+ the suspected one).

    The confirmed pair is looked up by its explicit human-labeled
    (doc_id, speaker_label); the suspected doc falls back to its
    largest-total_duration speaker. Reports honestly against whatever
    assignment EVoC produced — the gate that runs ``--validate`` decides
    whether a FAIL means rejecting the clustering, loosening
    ``--min-cluster-size``, or shipping the feature as experimental.
    """
    doc_ids = speakers["doc_id"].to_pylist()
    labels = speakers["speaker_label"].to_pylist()
    durations = speakers["total_duration"].to_pylist()

    def cluster_of(doc_id: str, label: str) -> int | None:
        for i, d in enumerate(doc_ids):
            if d == doc_id and labels[i] == label:
                return int(clusters[i])
        return None

    def main_speaker(doc_id: str) -> tuple[str, int] | None:
        """(speaker_label, cluster) of the doc's longest-speaking speaker."""
        best: tuple[float, str, int] | None = None
        for i, d in enumerate(doc_ids):
            if d == doc_id and (best is None or float(durations[i]) > best[0]):
                best = (float(durations[i]), labels[i], int(clusters[i]))
        return None if best is None else (best[1], best[2])

    def show(cluster: int) -> str:
        return "noise" if cluster < 0 else str(cluster)

    echo("Validation — known same-person pairs (human-labeled speakers):")
    (doc_a, label_a, tag_a), (doc_b, label_b, tag_b) = VALIDATION_CONFIRMED_PAIR
    cl_a = cluster_of(doc_a, label_a)
    cl_b = cluster_of(doc_b, label_b)
    if cl_a is None or cl_b is None:
        missing = ", ".join(tag for tag, found in ((tag_a, cl_a), (tag_b, cl_b)) if found is None)
        echo(f"  [FAIL] {tag_a} ↔ {tag_b}: no speakers row(s) for {missing}")
        return
    same = cl_a == cl_b and cl_a >= 0
    echo(
        f"  [{'PASS' if same else 'FAIL'}] {tag_a} labeled speaker ({label_a}, "
        f"cluster {show(cl_a)}) {'==' if same else '!='} {tag_b} labeled speaker "
        f"({label_b}, cluster {show(cl_b)})"
    )

    suspect_doc = doc_id_for_audio_path(db_path, table, VALIDATION_SUSPECTED_AUDIO)
    suspect = main_speaker(suspect_doc) if suspect_doc is not None else None
    if suspect is None:
        echo(f"  [info] suspected T0001786: no chunks/speakers row for {VALIDATION_SUSPECTED_AUDIO} — skipped")
        return
    label_s, cl_s = suspect
    echo(
        f"  [info] suspected T0001786 main speaker ({label_s}, cluster {show(cl_s)}) — "
        f"matches {tag_a}: {'yes' if cl_s >= 0 and cl_s == cl_a else 'no'}, "
        f"matches {tag_b}: {'yes' if cl_s >= 0 and cl_s == cl_b else 'no'} "
        "(suspected, not a hard FAIL)"
    )


class ClusterResult(BaseModel):
    """Outcome of :func:`cluster_speakers`, mirroring the CLI's summary lines.

    ``layer_idx`` / ``merge_rate`` are ``None`` when no layer met the false-merge
    bound and the run fell back to EVoC's own layer choice (``fallback=True``).
    """

    model_config = {"arbitrary_types_allowed": True}

    speakers: pa.Table
    clusters: np.ndarray
    n_layers: int
    layer_idx: int | None
    merge_rate: float | None
    fallback: bool
    fallback_merge_rate: float
    auto_merge_rate: float


def cluster_speakers(
    db: lancedb.DBConnection,
    db_path: Path,
    *,
    seed: int,
    min_cluster_size: int,
    on_start: Callable[[int], None] | None = None,
) -> ClusterResult:
    """Globally cluster speaker voiceprints → ``speakers.speaker_cluster`` (overwrite).

    Fits :class:`evoc.EVoC` over the ``speakers`` embedding matrix and rewrites the
    table wholesale with the new ``speaker_cluster`` column (mirrors
    ``build-speakers`` — the table is tiny). The written assignment is the layer
    :func:`select_identity_layer` picks, NOT EVoC's persistence-max ``labels_``.

    Raises :class:`ImportError` (with the atlas install hint) when ``evoc`` is
    missing, or :class:`ValueError` when ``speakers`` is missing/empty/NULL-valued.
    Returns a :class:`ClusterResult` carrying the table, the written labels, and the
    summary the CLI echoes.
    """
    from ratch.features.projection import _ATLAS_INSTALL_HINT

    try:
        import evoc  # ty: ignore[unresolved-import]
    except ImportError as e:
        raise ImportError(_ATLAS_INSTALL_HINT) from e
    import lance
    import numpy as np
    import pyarrow as pa

    from ratch.core.dataset import overwrite_dataset
    from ratch.model.schema import SPEAKERS_SCHEMA, VOICE_EMBED_DIM

    if "speakers" not in db.list_tables().tables:
        raise ValueError(f"Table 'speakers' not found in {db_path} — run `ratch build-speakers` first.")

    speakers_path = db_path / "speakers.lance"
    tbl = lance.dataset(str(speakers_path)).to_table()
    if tbl.num_rows == 0:
        raise ValueError("speakers is empty — run `ratch build-speakers` first.")
    emb = tbl["embedding"].combine_chunks()
    if emb.null_count > 0:
        raise ValueError(f"speakers.embedding has {emb.null_count} NULL row(s) — rebuild with `ratch build-speakers` first.")
    matrix = emb.flatten().to_numpy(zero_copy_only=False).reshape(-1, VOICE_EMBED_DIM).astype(np.float32)

    if on_start is not None:
        on_start(tbl.num_rows)

    clusterer = evoc.EVoC(base_min_cluster_size=min_cluster_size, random_state=seed)
    auto_labels = clusterer.fit_predict(matrix)
    doc_id_list: list[str] = tbl["doc_id"].to_pylist()
    selected = select_identity_layer(clusterer, doc_id_list)
    auto_merge_rate = same_doc_merge_rate(np.asarray(auto_labels, dtype=np.int32), doc_id_list)
    if selected is None:
        clusters = np.asarray(auto_labels, dtype=np.int32)
        layer_idx: int | None = None
        merge_rate: float | None = None
        fallback = True
        fallback_merge_rate = same_doc_merge_rate(clusters, doc_id_list)
    else:
        clusters, layer_idx, merge_rate = selected
        fallback = False
        fallback_merge_rate = 0.0

    out = tbl.set_column(
        tbl.schema.get_field_index("speaker_cluster"),
        SPEAKERS_SCHEMA.field("speaker_cluster"),
        pa.array(clusters, pa.int32()),
    ).cast(SPEAKERS_SCHEMA)
    overwrite_dataset(speakers_path, out)

    # The overwrite invalidates the BTREE index build-speakers made — rebuild it.
    speakers_tbl = db.open_table("speakers")
    try:
        speakers_tbl.create_scalar_index("doc_id", index_type="BTREE", replace=True)
        logger.info("rebuilt BTREE scalar index on speakers.doc_id")
    except Exception as e:
        logger.debug("scalar index (speakers.doc_id) skipped: %s", e)

    return ClusterResult(
        speakers=tbl,
        clusters=clusters,
        n_layers=len(clusterer.cluster_layers_),
        layer_idx=layer_idx,
        merge_rate=merge_rate,
        fallback=fallback,
        fallback_merge_rate=fallback_merge_rate,
        auto_merge_rate=auto_merge_rate,
    )
