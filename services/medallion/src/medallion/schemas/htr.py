"""The HTR cascade's GOLD schema contract — load-bearing, pinned at P7a (design §2.4).

Gold is the terminal transcription dataset the ``exporter`` service (P7c) projects consumer formats from
(ALTO 4.4 first). Every column here is something the serializer cannot reconstruct — **a field dropped
from gold is unrecoverable downstream** — so the silver→gold transcribe mover (P7b) must write exactly
this superset and the exporter must find it. The unit pin (``tests/unit/test_htr_gold_contract.py``)
freezes the list; changing it is a deliberate contract change, not a refactor.

The P7b movers on the unified cluster fill the lanes:

=============  ==================  ============================================  =====================
Edge           Trigger             Compute (mover stage job)                     Output
=============  ==================  ============================================  =====================
iiif→bronze    (ingest head)       ``ray_iiif_ingest_job.py`` (P7a producer)     bronze page-images
bronze→silver  ``medallion.bronze``  HTR layout+lines stage job (sealed runner)  region/line geometry
silver→gold    ``medallion.silver``  transcribe stage job (warm Serve handle)    THIS contract
=============  ==================  ============================================  =====================
"""

from __future__ import annotations


#: The gold transcription dataset's REQUIRED columns — everything the ALTO 4.4 serializer needs.
GOLD_CONTRACT_COLUMNS: tuple[str, ...] = (
    "volume_id",  # batch/bild id — the export unit
    "page_key",  # page identity within the volume (the ALTO filename stem)
    "page_width",  # pixel dims — ALTO Layout/Page attributes
    "page_height",
    "region_polygons",  # layout regions (TextBlock geometry)
    "line_polygons",  # text lines (TextLine geometry)
    "reading_order",  # line ordering within the page
    "text",  # the transcriptions, one per line
    "confidences",  # per-line model confidence (ALTO WC)
    "source_rowid",  # row-level provenance back to the bronze page (stable _rowid, rooted at bronze)
)
