"""The annotator's shutdown loop closes only slots the annotator populates (ANN-18).

`AppState` is the media plane's SHARED state shape, so it carries slots for every media service —
`embedder`/`reranker` are the search service's model handles and nothing in the annotator ever
assigns them. Closing them here was a guaranteed no-op that read as if this service owned models,
and a dead reference the estate's dead-code rule refuses.

Asserted on the SOURCE, not the runtime: importing `annotator.main` constructs the app (module-level
`FastAPI` + `DaprActor`), which a unit test has no business doing, and the defect is textual — the
names appear in the file at all.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_main_references_no_search_service_slots() -> None:
    spec = importlib.util.find_spec("annotator.main")
    assert spec is not None and spec.origin is not None
    source = Path(spec.origin).read_text()

    for foreign_slot in ("embedder", "reranker"):
        assert foreign_slot not in source, (
            f"annotator.main references AppState.{foreign_slot} — a search-service slot the annotator never populates; "
            "the shutdown loop must iterate only the resources this service opened"
        )
