"""The governed HTR lane's stage (#88 step 4): bronze pages → Serve → parse → ``gold$htr``.

Mirrors :func:`compute.transform_stage`'s contract exactly — same signature family, same write
flags, same lineage-in-the-same-commit rule (R26), same ``WriteResult`` back to the mover's emit —
because the mover treats stages uniformly and the emit path must not care which lane ran. What
differs is the TRANSFORM: instead of deriving thumbnails, each page's payload goes to the deployed
``/htrflow`` ingress and the returned ALTO is parsed into the gold contract's payload columns.

The gold schema is DERIVED FROM ``GOLD_CONTRACT_COLUMNS`` (via ``htr_parse``'s import-time guard),
so the table this writes and the contract cannot disagree without the module refusing to import.

IN-PROCESS ONLY, stated: this lane does not ride ``submit_stage_job``'s distributed path yet — the
generic Ray stage job derives thumbnails and knows nothing of Serve or ALTO. The P7b re-cut moves
this transform onto the cluster; until then the mover runs it in-process even when ``ray_enabled``
is on, which `transform.py`'s dispatch makes explicit rather than silently wrong.
"""

from __future__ import annotations

import logging

import lance
import pyarrow as pa
from lineage_kit.consume import LineageDoc

from medallion.services import catalog_register, htr_parse, htr_transcribe
from medallion.services.compute import _LINEAGE_COLUMN, WriteResult, _index_lineage, _lineage_column, measure
from service_kit.lakehouse import blobs


log = logging.getLogger(__name__)

#: The mover ``operation`` value that routes a stage through THIS lane (the chart's movers entry).
HTR_OPERATION = "transcribe_pages"

_STAGE_COLUMN = "stage"


def _split_source_uri(source_uri: str) -> tuple[str, str]:
    """``iiif://vol/00012.jpg`` → (``iiif://vol``, ``00012.jpg``).

    The bronze page schema carries identity as ONE ``source_uri`` column; the gold contract wants
    it split (``volume_id`` is the export unit, ``page_key`` the ALTO filename stem). The last path
    segment is the page — the same rule the ingest lane used to compose the URI.
    """
    base, _, leaf = source_uri.rpartition("/")
    return (base, leaf) if base else ("", source_uri)


def transcribe_stage(
    from_uri: str,
    to_uri: str,
    storage_options: dict[str, str],
    *,
    stage: str,
    lineage: LineageDoc | None = None,
    htrflow_url: str,
    timeout_seconds: float = 600.0,
    catalog_url: str = "",
    catalog_root: str = "",
    table_id: str = "",
    catalog_token: str | None = None,
    delimiter: str = "$",
) -> WriteResult:
    """Transcribe every bronze page into one gold row; ONE overwrite commit, lineage included.

    Reads through ``read_aligned_table`` with ``with_row_id`` — cardinality-preserving (the blob-v2
    lesson) and the source of ``source_rowid``, the row-level provenance rooting every gold row at
    the exact bronze row it descends from. A page whose payload is NULL fails the STAGE loudly,
    naming the page: bronze's page schema declares payload non-null, so a null here is an upstream
    corruption no half-written gold table should paper over.
    """
    ds = lance.dataset(from_uri, storage_options=storage_options)
    rows = blobs.read_aligned_table(ds, columns=["source_uri", "payload"], with_row_id=True)
    payloads = rows.column("payload").to_pylist()
    source_uris = rows.column("source_uri").to_pylist()
    row_ids = rows.column("_rowid").to_pylist()

    pages: list[htr_parse.ParsedPage] = []
    volume_ids: list[str] = []
    page_keys: list[str] = []
    for source_uri, payload in zip(source_uris, payloads, strict=True):
        if payload is None:
            raise ValueError(f"bronze page {source_uri!r} has a NULL payload — refusing to write a gold row for it")
        volume_id, page_key = _split_source_uri(str(source_uri))
        alto = htr_transcribe.transcribe_page(payload, base_url=htrflow_url, name=page_key, timeout_seconds=timeout_seconds)
        pages.append(htr_parse.parse_alto(alto))
        volume_ids.append(volume_id)
        page_keys.append(page_key)

    out = pa.table(
        {
            "id": pa.array(range(len(pages)), pa.int64()),
            "volume_id": pa.array(volume_ids, pa.string()),
            # The key the LANE derived, not the ALTO's echo of ?name= — one authority. The parsed
            # copy is what a reader of the raw document sees; asserting they agree is the parser's
            # test's job, not a runtime branch.
            "page_key": pa.array(page_keys, pa.string()),
            "page_width": pa.array([p.page_width for p in pages], pa.int64()),
            "page_height": pa.array([p.page_height for p in pages], pa.int64()),
            "region_polygons": pa.array([p.region_polygons for p in pages], pa.list_(pa.string())),
            "line_polygons": pa.array([p.line_polygons for p in pages], pa.list_(pa.string())),
            "reading_order": pa.array([p.reading_order for p in pages], pa.list_(pa.int64())),
            "text": pa.array([p.text for p in pages], pa.list_(pa.string())),
            "confidences": pa.array([p.confidences for p in pages], pa.list_(pa.float64())),
            "source_rowid": pa.array(row_ids, pa.int64()),
            _STAGE_COLUMN: pa.array([stage] * len(pages), pa.string()),
        }
    )
    if lineage is not None:
        # Same commit as the data (R26) — a governed row must never be readable without provenance.
        out = out.append_column(pa.field(_LINEAGE_COLUMN, pa.json_()), _lineage_column(lineage, out.num_rows))

    lance.write_dataset(
        out,
        to_uri,
        mode="overwrite",
        storage_options=storage_options,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    if lineage is not None:
        _index_lineage(to_uri, storage_options)

    # REGISTRATION IS PART OF THE STAGE (#88 step 5): a gold table the catalog never heard of is
    # the defect this lane exists to close, so an unregistered write must not report success. Runs
    # AFTER the write (register_table describes what exists) and BEFORE the emit; 409 = already
    # governed (idempotent redelivery). table_id empty = the caller opted out (tests of the pure
    # transform); the MOVER always passes it.
    if table_id:
        catalog_register.register_stage_output(
            catalog_url=catalog_url,
            catalog_root=catalog_root,
            table_id=table_id,
            to_uri=to_uri,
            delimiter=delimiter,
            token=catalog_token,
        )

    result = measure(to_uri, storage_options)
    # Every payload column is a TRANSFORMATION of the page bytes; identity columns derive from
    # source_uri; source_rowid is the bronze row's own id (IDENTITY). stage/lineage are this run's
    # stamps — no input column, no edge (same rule as the generic stage).
    result.column_map = [
        *[
            (name, "payload", "TRANSFORMATION")
            for name in ("page_width", "page_height", "region_polygons", "line_polygons", "reading_order", "text", "confidences")
        ],
        ("volume_id", "source_uri", "TRANSFORMATION"),
        ("page_key", "source_uri", "TRANSFORMATION"),
        ("source_rowid", "source_uri", "IDENTITY"),
    ]
    # The run's own provenance rides the RESULT to the emit (#88 step 6) — parsed from the ALTO,
    # de-duplicated preserving order across pages; one build produced the batch, so the first sha
    # is the sha (a mixed-sha batch would mean Serve redeployed MID-STAGE — worth surfacing, not
    # averaging away, hence taking first + logging the set below).
    result.models = list(dict.fromkeys(m for p in pages for m in p.models))
    result.commit_sha = next((p.commit_sha for p in pages if p.commit_sha), None)
    log.info(
        "htr_stage_written",
        extra={"to_uri": to_uri, "pages": len(pages), "models": sorted({m for p in pages for m in p.models})},
    )
    return result
