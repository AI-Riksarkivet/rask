"""Read a live index's parameterisation back, well enough to rebuild the SAME index.

[[LH-105]]. A repair that re-tunes what it repairs is the failure this module exists to prevent: the
table keeps answering queries, so the only symptom of a silently re-partitioned vector index is recall
or latency that nobody attributes to the repair.

**IT TAKES TWO CALLS, and that is the non-obvious part.** Measured on pylance 11.0.0 (the version the
deployed catalog runs), `describe_indices()` — the non-deprecated call — carries `.name`,
`.index_type`, `.field_names`, `.type_url` and `.details`, where a vector index's `.details` is
``{"metric_type": ..., "compression": {"num_bits": ..., "num_sub_vectors": ...}, "runtime_hints": {...}}``.
`num_partitions` is NOT among them; it appears only as ``indices[0].num_partitions`` in
``index_statistics(name)``. Reading one call is therefore not a smaller version of reading both — it
is a rebuild that quietly re-partitions.

**THE KIND IS OBSERVED, NOT INFERRED.** `.type_url` is ``/lance.index.pb.VectorIndexDetails`` for a
vector index and ``/lance.table.<Kind>IndexDetails`` for every scalar one, so the vector/scalar
decision comes off the live index rather than from a list of type names that a newly-added index type
would silently fall out of — the failure `IndexWorkItem.kind` warns about, where a worker guessing
between the two builds an index that answers queries wrongly rather than not at all.

`runtime_hints` are deliberately NOT carried back: they are the tuning of the build that ran
(k-means iteration counts, sample rates, shuffle concurrency), not of the index that resulted, and
pylance takes them from its own environment rather than as `create_index` keywords.

All IO is blocking; callers threadpool it.
"""

from __future__ import annotations

import json
from typing import Any

from lance.query import DocumentGranularity
from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse.work_items import SCALAR_INDEX, SCALAR_INDEX_TYPES, VECTOR_INDEX


#: `.type_url`'s vector marker. Scalar kinds each carry their own `/lance.table.<Kind>IndexDetails`,
#: so the discriminator is written as "vector, else scalar" rather than as a list of scalar spellings
#: that a new scalar type would have to be added to.
_VECTOR_TYPE_URL = "/lance.index.pb.VectorIndexDetails"

#: `.details` keys that are not `create_index` keywords. `runtime_hints` describes the BUILD that ran
#: rather than the index that resulted, and `compression` is unpacked into its own keywords below.
_NON_KEYWORD_DETAILS = frozenset({"runtime_hints", "compression"})

#: The estate's scalar vocabulary, keyed the way `describe_indices()` spells it back.
#:
#: **A READBACK IS NOT IN THE VOCABULARY IT IS STAMPED INTO, and `.upper()` is not the bridge.**
#: Measured on pylance 11.0.0 across every scalar type it would build: `BITMAP`->`Bitmap`,
#: `BTREE`->`BTree`, `INVERTED`->`Inverted`, `NGRAM`->`NGram`, `ZONEMAP`->`ZoneMap`,
#: `BLOOMFILTER`->`BloomFilter` — and `LABEL_LIST`->`LabelList`, which upper-cases to `LABELLIST` and
#: is the one spelling `SCALAR_INDEX_TYPES` does not contain. Underscores are dropped on both sides
#: rather than a hand-written map, so a scalar type added upstream lands correctly without an edit
#: here; anything genuinely unknown falls through unchanged and the worker refuses it by name.
_SCALAR_TYPE_BY_READBACK = {kind.replace("_", "").lower(): kind for kind in SCALAR_INDEX_TYPES}

#: Readback keys whose VALUE has to be rebuilt into the type the signature declares.
#:
#: `create_scalar_index` takes `**kwargs` and forwards almost everything to Rust untouched, so the
#: readback's plain JSON is a usable keyword for eighteen of an inverted index's nineteen fields. The
#: exception is a parameter pylance names EXPLICITLY and type-checks in Python: `.details` reports
#: `document_granularity` as the string `"row"` and the signature refuses anything that is not a
#: `lance.query.DocumentGranularity`, so the rebuild dies `TypeError` inside pylance. Coerced rather
#: than dropped because it is real fidelity — row versus list-element granularity changes what a
#: phrase query matches, which is precisely the silent re-tune this module exists to prevent. The
#: enum subclasses `str`, so the readback's own spelling reconstructs it exactly.
_DETAIL_VALUE_TYPES: dict[str, type] = {"document_granularity": DocumentGranularity}


class IndexNotFoundForRebuildError(LookupError):
    """The named index is not on this dataset.

    Raised rather than defaulted: the caller asked to REPAIR a named index, so minting one from
    pylance's defaults would be the silent re-tune this module exists to prevent — and it would do it
    on a table where the operator believes an index already exists.
    """


class RebuildSpec(BaseModel):
    """Everything `create_index` / `create_scalar_index` needs to produce this index again."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    column: str
    #: ``vector`` or ``scalar`` — which pylance call, read off `.type_url`.
    kind: str
    #: Lance's own vocabulary (``IVF_PQ``, ``BTREE``, ``Inverted``, …), forwarded verbatim. Measured
    #: accepted in the spelling `describe_indices()` reports it, mixed case included.
    index_type: str
    #: pylance's own keyword arguments, ready to splat. Empty for the scalar kinds that carry no
    #: build parameter beyond the column, which is full fidelity for them rather than a gap.
    params: dict[str, Any] = {}


def describe_index_for_rebuild(dataset: Any, index_name: str) -> RebuildSpec:
    """Read `index_name` off `dataset` as the arguments that would rebuild it.

    Args:
        dataset: An open `lance.LanceDataset`.
        index_name: The index to describe, as `ListTableIndices` names it.

    Returns:
        The call shape that reproduces this index, with `params` ready to splat as keywords.

    Raises:
        IndexNotFoundForRebuildError: No index of that name is on the dataset.
    """
    described = next((d for d in dataset.describe_indices() if d.name == index_name), None)
    if described is None:
        present = sorted(d.name for d in dataset.describe_indices())
        raise IndexNotFoundForRebuildError(f"no index named {index_name!r} on {dataset.uri}; it has {present}")

    details = _usable_details(described.details or {})
    is_vector = described.type_url == _VECTOR_TYPE_URL
    params = _vector_params(dataset, index_name, details) if is_vector else {k: v for k, v in details.items() if k not in _NON_KEYWORD_DETAILS}
    return RebuildSpec(
        name=described.name,
        # A multi-column index cannot be rebuilt through a single-column call, and pylance's own
        # create doors take one column; naming the first would rebuild a narrower index than the one
        # being repaired, so it is refused rather than truncated.
        column=_sole_column(described.field_names, index_name),
        kind=VECTOR_INDEX if is_vector else SCALAR_INDEX,
        index_type=described.index_type if is_vector else _scalar_type(described.index_type),
        params=params,
    )


def _usable_details(details: dict[str, Any]) -> dict[str, Any]:
    """The readback, as values pylance will accept back.

    A `None` is DROPPED rather than forwarded, the same rule `indices._pylance_kwargs` states for the
    create doors: pylance's defaults are meaningful, and an explicit `None` overrides them with
    something nobody asked for. Here it is also indistinguishable from "this index did not set it" —
    an inverted index reads back `lance_tokenizer: None` and `custom_stop_words: None` whether or not
    they were ever chosen.
    """
    usable: dict[str, Any] = {}
    for key, value in details.items():
        if value is None:
            continue
        coerce = _DETAIL_VALUE_TYPES.get(key)
        usable[key] = coerce(value) if coerce is not None else value
    return usable


def _scalar_type(reported: str) -> str:
    """pylance's display spelling, as the vocabulary the work item carries and the worker accepts."""
    return _SCALAR_TYPE_BY_READBACK.get(reported.replace("_", "").lower(), reported)


def _sole_column(field_names: list[str], index_name: str) -> str:
    if len(field_names) != 1:
        raise IndexNotFoundForRebuildError(f"index {index_name!r} covers {field_names}; a rebuild through pylance's create doors takes exactly one column")
    return field_names[0]


def _vector_params(dataset: Any, index_name: str, details: dict[str, Any]) -> dict[str, Any]:
    """Vector tuning, assembled from BOTH reads — `.details` plus the statistics that hold `num_partitions`.

    `metric` rather than `metric_type`, and `num_sub_vectors` lifted out of `compression`, because the
    keyword names `create_index` answers to are not the field names the readback reports.
    """
    params: dict[str, Any] = {k: v for k, v in details.items() if k not in _NON_KEYWORD_DETAILS}
    if (metric := params.pop("metric_type", None)) is not None:
        params["metric"] = metric
    compression: dict[str, Any] = dict(details.get("compression") or {})
    if (sub_vectors := compression.get("num_sub_vectors")) is not None:
        params["num_sub_vectors"] = sub_vectors
    if (num_bits := compression.get("num_bits")) is not None:
        params["num_bits"] = num_bits
    if (partitions := _partition_count(dataset, index_name)) is not None:
        params["num_partitions"] = partitions
    return params


def _partition_count(dataset: Any, index_name: str) -> int | None:
    """`num_partitions`, which lives only in the statistics call.

    Best-effort on purpose: the statistics payload is a JSON blob whose shape pylance has already
    announced it will change (it currently embeds centroids and warns that it will stop). Losing the
    partition count is a worse index; failing the repair outright over a shape change is a worse
    outage, and the caller can still supply the value.
    """
    try:
        stats = json.loads(dataset._ds.index_statistics(index_name))
        indices = stats.get("indices") or []
        partitions = indices[0].get("num_partitions") if indices else None
    except (KeyError, IndexError, ValueError, AttributeError, RuntimeError):
        return None
    return int(partitions) if isinstance(partitions, int) else None
