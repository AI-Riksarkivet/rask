"""EVōC 2-D layout + cluster columns for the Embedding Atlas view.

Attaches three scalar columns to ``chunks`` from ``text_embedding``:

* ``atlas_x`` / ``atlas_y`` — a 2-D layout for the scatter map, built from
  EVōC's own building blocks (``knn_graph → neighbor_graph_matrix →
  node_embedding(n_components=2)``) — the same chain ``evoc.evoc_clusters``
  runs internally, but emitted at 2 dimensions for visualisation.
* ``atlas_cluster`` — a semantic cluster id (``-1`` = noise) from the public
  :class:`evoc.EVoC` estimator, used as a "colour by cluster" option in the UI.

The triplet is **namespaced** by ``column_prefix`` so a second, parallel
projection can coexist: the default ``'atlas_'`` (text, from ``text_embedding``)
and ``'atlas_img_'`` (visual, from a chunk-level ``frame_embedding``) write
``atlas_x/y/cluster`` vs ``atlas_img_x/y/cluster`` respectively. The two cluster
id spaces are independent EVōC fits and never comparable.

Unlike the per-batch features in :mod:`ratch.features.columns`, every row's
value depends on *all* rows (a single global fit), so this can't run as an
``add_columns`` UDF. It mirrors :func:`upsert_blob_column`'s two-pass shape:
one ``with_row_id=True`` scan loads the whole embedding matrix, EVōC fits once,
then ``add_columns`` passes attach the precomputed columns *by* ``_rowid``.

``add_columns`` writes one new column file alongside the existing fragments (a
metadata-only op — never the ``merge_insert`` join that crashes on the wide
``chunks`` schema; see ``model/schema.py``), so this is safe to run on
``chunks`` next to ``text_embedding`` itself.

EVōC is an optional dependency (the ``atlas`` extra); it is imported lazily so
an FTS-only or search-only deployment never needs it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import lance
import numpy as np
import pyarrow as pa


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Default (text-space) namespace prefix; the X/Y/cluster column names derive
#: from it (``atlas_x`` / ``atlas_y`` / ``atlas_cluster``). The visual space
#: passes ``column_prefix='atlas_img_'`` to get a parallel, non-clashing triplet.
DEFAULT_ATLAS_PREFIX = "atlas_"
ATLAS_X_COLUMN = "atlas_x"
ATLAS_Y_COLUMN = "atlas_y"
ATLAS_CLUSTER_COLUMN = "atlas_cluster"

#: EVōC neighbour count for both the kNN graph (layout) and the clusterer.
#: 15 is EVōC's own default — a good local/global balance.
DEFAULT_N_NEIGHBORS = 15
#: node_embedding optimisation epochs for the 2-D layout (EVōC default).
DEFAULT_N_EPOCHS = 50


def atlas_columns(column_prefix: str = DEFAULT_ATLAS_PREFIX) -> tuple[str, str, str]:
    """The ``(x, y, cluster)`` column names for a projection namespace.

    ``'atlas_'`` → ``('atlas_x', 'atlas_y', 'atlas_cluster')`` (text, default);
    ``'atlas_img_'`` → ``('atlas_img_x', 'atlas_img_y', 'atlas_img_cluster')``
    (visual). The cluster ids of the two spaces are NOT comparable — each is an
    independent EVōC fit — so they deliberately live in separate columns.
    """
    return f"{column_prefix}x", f"{column_prefix}y", f"{column_prefix}cluster"


def project_atlas_columns(
    chunks_path: str | Path,
    *,
    source_column: str = "text_embedding",
    column_prefix: str = DEFAULT_ATLAS_PREFIX,
    n_neighbors: int = DEFAULT_N_NEIGHBORS,
    n_epochs: int = DEFAULT_N_EPOCHS,
    seed: int | None = None,
    with_clusters: bool = True,
    batch_rows: int = 4096,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Fit EVōC and attach ``{prefix}x`` / ``{prefix}y`` / ``{prefix}cluster``.

    ``column_prefix`` namespaces the output triplet so a second projection (e.g.
    the visual ``frame_embedding`` space at ``column_prefix='atlas_img_'``) can
    coexist with the default text space. ``source_column`` chooses the embedding
    column EVōC fits over.

    Returns the number of rows projected (``0`` when the columns already exist
    and ``overwrite`` is ``False``). Raises :class:`ValueError` if
    ``source_column`` is absent or has any ``NULL`` rows — EVōC can't fit a
    partially-embedded table, so the failure is loud and actionable.
    """
    x_col, y_col, cluster_col = atlas_columns(column_prefix)
    ds = lance.dataset(str(chunks_path))

    if source_column not in ds.schema.names:
        raise ValueError(f"'{source_column}' column missing — run `ratch feature {source_column}` (or `frame_embedding` for the visual space) first")

    targets = [x_col, y_col] + ([cluster_col] if with_clusters else [])
    if all(c in ds.schema.names for c in targets) and not overwrite:
        logger.info("%s already present — pass --all to rebuild", ", ".join(targets))
        return 0

    row_ids, matrix = _load_embedding_matrix(ds, source_column, batch_rows)
    if matrix.shape[0] == 0:
        return 0

    xy = _evoc_layout(matrix, n_neighbors=n_neighbors, n_epochs=n_epochs, seed=seed)
    clusters = _evoc_clusters(matrix, n_neighbors=n_neighbors, seed=seed) if with_clusters else None
    del matrix  # ~1 GB for 145k × 2048 float32 — free before the attach passes

    _attach_column_by_row_id(
        chunks_path,
        x_col,
        value_by_row_id=dict(zip(row_ids, xy[:, 0].tolist(), strict=True)),
        pa_type=pa.float32(),
        batch_rows=batch_rows,
    )
    _attach_column_by_row_id(
        chunks_path,
        y_col,
        value_by_row_id=dict(zip(row_ids, xy[:, 1].tolist(), strict=True)),
        pa_type=pa.float32(),
        batch_rows=batch_rows,
        progress=None if clusters is not None else progress,
    )
    if clusters is not None:
        _attach_column_by_row_id(
            chunks_path,
            cluster_col,
            value_by_row_id=dict(zip(row_ids, clusters.tolist(), strict=True)),
            pa_type=pa.int32(),
            batch_rows=batch_rows,
            progress=progress,
        )
    return len(row_ids)


def _load_embedding_matrix(ds: lance.LanceDataset, source_column: str, batch_rows: int) -> tuple[list[int], np.ndarray]:
    """Scan the whole embedding column once into ``(row_ids, (N, dim) float32)``."""
    dim = ds.schema.field(source_column).type.list_size
    row_ids: list[int] = []
    blocks: list[np.ndarray] = []
    for batch in ds.to_batches(columns=[source_column], with_row_id=True, batch_size=batch_rows):
        col = batch.column(source_column)
        if col.null_count > 0:
            raise ValueError(f"'{source_column}' has {col.null_count} NULL row(s) in this batch — every row needs an embedding before projecting")
        flat = col.flatten().to_numpy(zero_copy_only=False).astype(np.float32, copy=False)
        blocks.append(flat.reshape(len(col), dim))
        row_ids.extend(batch.column("_rowid").to_pylist())
    matrix = np.vstack(blocks) if blocks else np.empty((0, dim), dtype=np.float32)
    logger.info("loaded embedding matrix %s for EVōC", matrix.shape)
    return row_ids, matrix


def _evoc_layout(matrix: np.ndarray, *, n_neighbors: int, n_epochs: int, seed: int | None) -> np.ndarray:
    """2-D layout via EVōC's kNN-graph → node-embedding chain → ``(N, 2)`` float32."""
    try:
        from evoc.graph_construction import neighbor_graph_matrix  # ty: ignore[unresolved-import]
        from evoc.knn_graph import knn_graph  # ty: ignore[unresolved-import]
        from evoc.node_embedding import node_embedding  # ty: ignore[unresolved-import]
    except ImportError as e:
        raise ImportError(_ATLAS_INSTALL_HINT) from e
    from sklearn.utils import check_random_state  # ty: ignore[unresolved-import]

    rng = check_random_state(seed)
    logger.info("EVōC kNN graph (n_neighbors=%d) on %d row(s)…", n_neighbors, matrix.shape[0])
    nn_inds, nn_dists = knn_graph(matrix, n_neighbors=n_neighbors, random_state=rng)
    graph = neighbor_graph_matrix(n_neighbors, nn_inds, nn_dists, symmetrize=True)

    initial = None
    try:
        from evoc.label_propagation import label_propagation_init  # ty: ignore[unresolved-import]

        initial = label_propagation_init(graph, n_components=2, data=matrix, random_state=rng)
    except Exception as e:
        logger.warning("EVōC label-prop init failed (%s); using random init", e)

    logger.info("EVōC node embedding → 2-D (n_epochs=%d)…", n_epochs)
    xy = node_embedding(
        graph,
        n_components=2,
        n_epochs=n_epochs,
        initial_embedding=initial,
        random_state=rng,
        reproducible_flag=seed is not None,
    )
    return np.asarray(xy, dtype=np.float32)


def _evoc_clusters(matrix: np.ndarray, *, n_neighbors: int, seed: int | None) -> np.ndarray:
    """Semantic cluster ids (``-1`` = noise) via the public EVōC estimator."""
    try:
        import evoc  # ty: ignore[unresolved-import]
    except ImportError as e:
        raise ImportError(_ATLAS_INSTALL_HINT) from e

    logger.info("EVōC clustering %d row(s)…", matrix.shape[0])
    labels = evoc.EVoC(n_neighbors=n_neighbors, random_state=seed).fit_predict(matrix)
    n_clusters = int(labels.max()) + 1 if labels.size and labels.max() >= 0 else 0
    logger.info("EVōC found %d cluster(s) (+ noise)", n_clusters)
    return np.asarray(labels, dtype=np.int32)


def _attach_column_by_row_id(
    chunks_path: str | Path,
    name: str,
    *,
    value_by_row_id: dict[int, Any],
    pa_type: pa.DataType,
    batch_rows: int,
    progress: Callable[[int], None] | None = None,
) -> None:
    """Attach a precomputed column keyed by ``_rowid`` via ``add_columns``.

    Mirrors :func:`ratch.core.engine.upsert_blob_column`'s attach pass: a
    ``batch_udf`` reads ``_rowid`` and places each precomputed value. A missing
    id raises (loud) rather than misaligning. Drops an existing column first so
    ``--all`` rebuilds cleanly.
    """
    ds = lance.dataset(str(chunks_path))
    if name in ds.schema.names:
        ds.drop_columns([name])
        ds = lance.dataset(str(chunks_path))

    schema = pa.schema([pa.field(name, pa_type, nullable=True)])

    @lance.batch_udf(output_schema=schema)
    def attach(batch: pa.RecordBatch) -> pa.RecordBatch:
        row_ids = batch.column("_rowid").to_pylist()
        out = pa.array([value_by_row_id[r] for r in row_ids], type=pa_type)
        if progress is not None:
            progress(len(row_ids))
        return pa.RecordBatch.from_arrays([out], names=[name])

    logger.info("attaching column %s for %d row(s)", name, len(value_by_row_id))
    ds.add_columns(attach, read_columns=["_rowid"], batch_size=batch_rows)


_ATLAS_INSTALL_HINT = "The Atlas projection needs the `atlas` extra (EVōC). Install with `uv sync --extra atlas` (or `pip install 'ratch[atlas]'`)."
