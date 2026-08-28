"""Topics endpoint — serve the precomputed topic hierarchy for the Tree page.

Port of the old ``backend/topics/router.py``: the topics table (and its JSONB
hierarchy column) now come from the descriptor's ``topics`` capability
(``table.column``), the dataset from the optional ``dataset`` query param.
The table is one tiny row, read on demand per request so a topics rebuild is
picked up without restarting the server; an undeclared capability or absent
table returns ``built: false``.

The topic *filter* lives on the search group (``topic=`` matches any topic
layer column); this route is only the hierarchy.

No ``from __future__ import annotations`` here: FastAPI introspects these
signatures at runtime, so the annotations stay real objects.
"""

import json
from typing import Any

import lance
from fastapi import APIRouter

from service_kit.lancekit import store
from service_kit.lancekit.topics_meta import NOISE_LABEL
from service_kit.media.deps import StateDep
from service_kit.media.state import dataset_handle
from viewer.api.security import REQUIRE_CORPUS_DATA
from viewer.schemas.topics import TopicsResponse


router = APIRouter(prefix="/api/topics", tags=["topics"])

# Single-row writer contract of the topics capability: build_topic_tree always
# writes exactly one row with these two columns non-null (the hierarchy column
# name itself comes from the capability declaration).
_LAYERS = "layers"
_N_CHUNKS = "n_chunks"
_DEFAULT_HIERARCHY_COLUMN = "hierarchy"

_NOT_BUILT = TopicsResponse(built=False, noise_label=NOISE_LABEL)


def _decode_jsonb(raw: Any) -> Any:
    """Lance JSONB hands back either the raw JSON string or an already-decoded value."""
    return json.loads(raw) if isinstance(raw, str) else raw


@router.get("", dependencies=[REQUIRE_CORPUS_DATA])
def get_topics(state: StateDep, dataset: str | None = None) -> TopicsResponse:
    """The topic hierarchy for the treemap, or ``built: false`` if not generated yet."""
    handle = dataset_handle(state, dataset)
    target = handle.descriptor.declared.capabilities.get("topics")
    if target is None:
        return _NOT_BUILT
    table, _, column = target.partition(".")
    uri = handle.table_uri(table)
    if not store.exists(uri, handle.storage_options):
        return _NOT_BUILT
    rows = lance.dataset(uri, storage_options=handle.storage_options).to_table().to_pylist()
    if not rows:
        return _NOT_BUILT
    # The columns are a writer-guaranteed contract (build_topic_tree always
    # writes all three non-null), so index directly — a schema drift should
    # 500 loudly, not silently return a payload the treemap can't render.
    row = rows[0]
    return TopicsResponse(
        built=True,
        layers=int(row[_LAYERS]),
        n_chunks=int(row[_N_CHUNKS]),
        hierarchy=_decode_jsonb(row[column or _DEFAULT_HIERARCHY_COLUMN]),
        noise_label=NOISE_LABEL,
    )
