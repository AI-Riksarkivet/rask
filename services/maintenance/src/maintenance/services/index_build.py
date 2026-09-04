"""Build one index, off the request path — `open_lakehouse_lanes.md`, the index half.

The catalog's `create_index` / `create_scalar_index` doors ran the whole build in their own handler,
so the cost of a request was a property of the table rather than of the request. **The spec asks for
the opposite**: `CreateTableIndex` states that "index creation is handled asynchronously" and that
progress is read through `ListTableIndices` / `DescribeTableIndexStats`, and its response carries an
optional `transaction_id` and nothing else.

**The WHOLE build crosses to this worker, rather than a plan.** Compaction splits its three phases
across processes because `CompactionTask` and `RewriteResult` round-trip through `.json()`. Measured
on pylance 10.0.0 (2026-09-04): an index segment — what `create_index_uncommitted` returns — carries
no `json`, `to_json` or `serialize`, so the `create_index_uncommitted` →
`merge_existing_index_segments` → `commit_existing_index_segments` chain cannot be spread today. It
runs here in one process, which still takes it off the request path, and the finer split becomes
available the day those segments serialize.

The write is signed by the credential the caller resolved — a rewrite is a write, and an index build
lands files under the table's own prefix, so it takes the same per-table vending path compaction does.

All IO is blocking; callers threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final, Literal, cast

import lance
from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse.work_items import SCALAR_INDEX, VECTOR_INDEX, IndexWorkItem


log = logging.getLogger(__name__)

#: pylance types `create_scalar_index`'s `index_type` as a Literal union; this is that union, named so
#: the narrowing above reads as one check against one list.
ScalarIndexType = Literal["BTREE", "BITMAP", "LABEL_LIST", "INVERTED", "FTS", "NGRAM", "ZONEMAP", "BLOOMFILTER", "RTREE"]


#: The scalar index types pylance accepts, as its own signature declares them. Held here so an unknown
#: value arriving off a broker is a clean REFUSAL rather than a `TypeError` deep inside pylance — and
#: so the narrowing that satisfies the typed signature is a real check rather than a cast that asserts
#: something nobody verified.
SCALAR_INDEX_TYPES: Final = frozenset({"BTREE", "BITMAP", "LABEL_LIST", "INVERTED", "FTS", "NGRAM", "ZONEMAP", "BLOOMFILTER", "RTREE"})


class IndexOutcome(BaseModel):
    """What the build produced, named the way `ListTableIndices` names it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    column: str
    kind: str
    #: The dataset version the index was committed at, so a caller can tell a rebuild from a no-op.
    version: int


class UnknownIndexKindError(ValueError):
    """The unit named a door — or a scalar index type — this worker cannot build.

    Refused rather than defaulted: building a scalar index where a vector one was asked for produces a
    table that answers queries wrongly rather than not at all, which is the worse failure and the
    harder one to notice. It is ACKED rather than retried at the route, because a producer defect is
    the one thing redelivery cannot repair.
    """


def build_index(item: IndexWorkItem, *, write_options: Mapping[str, str]) -> IndexOutcome:
    """Build the index this unit describes and return what landed.

    `params` are forwarded as keyword arguments and never inspected: they are pylance's own index
    tuning (`num_partitions`, `num_sub_vectors`, `metric`, …), already translated from the spec's
    field names by the door that published the unit. This module has no opinion on them, the same rule
    a transform's params follow.

    `replace` is deliberately not passed: the spec's request has no such field, so no caller can ask
    for it, and pylance's own defaults then apply (scalar replaces, vector refuses a duplicate name).
    Passing a value nobody chose would make this worker decide a semantics the door never offered.
    """
    dataset = lance.dataset(item.uri, storage_options=dict(write_options) or None)
    kwargs: dict[str, Any] = dict(item.params)
    if item.name:
        kwargs["name"] = item.name
    if item.kind == VECTOR_INDEX:
        dataset.create_index(item.column, index_type=item.index_type, **kwargs)
    elif item.kind == SCALAR_INDEX:
        if item.index_type not in SCALAR_INDEX_TYPES:
            raise UnknownIndexKindError(f"unknown scalar index type {item.index_type!r}; pylance accepts {sorted(SCALAR_INDEX_TYPES)}")
        dataset.create_scalar_index(item.column, index_type=cast(ScalarIndexType, item.index_type), **kwargs)
    else:
        raise UnknownIndexKindError(f"unknown index kind {item.kind!r}; this worker builds {sorted((VECTOR_INDEX, SCALAR_INDEX))}")
    # RE-OPENED, because the build commits a new manifest and the handle above still describes the
    # version it started from. Reporting that one would tell a caller their index landed at a version
    # that does not contain it.
    committed = lance.dataset(item.uri, storage_options=dict(write_options) or None)
    log.info(
        "index_built",
        extra={
            "uri": item.uri,
            "table_id": item.table_id,
            "column": item.column,
            "kind": item.kind,
            "index_type": item.index_type,
            "version": committed.version,
        },
    )
    return IndexOutcome(name=item.name or f"{item.column}_idx", column=item.column, kind=item.kind, version=int(committed.version))
