"""Content-dispatched artifact derivation — how the cascade is multimodal WITHOUT the platform knowing.

The lakehouse operates on LANCE TYPES only: tabular columns flow as tabular, vector columns as vectors,
and a blob column is bytes of ANY media kind (image, audio, video, docs — ``lance_docs/guide.md`` blob
chapter). The generic compute carries all of them forward; this module is the single place that decides
what a stage can DERIVE from blob payloads, by probing the CONTENT (not config, not column names, not
dataset names):

* image payloads → the inline artifacts (``thumbnail`` PNG + ``embedding`` fixed-size floats)
* anything unrecognised → carried forward untouched (an audio deriver slots into ``_DERIVERS`` later)
* no blob columns at all (tabular datasets) → a no-op

So EVERY stage of EVERY lane runs the same transform and "just works" per dataset content — no
per-mover media config, no modality branches in the platform.
"""

from __future__ import annotations

from collections.abc import Callable

import pyarrow as pa

from service_kit.lakehouse import media


#: A per-modality deriver: the carried table + one blob column's payloads -> the table with artifacts.
#: A payload is ``None`` where the upstream row's blob is null (an un-harvested page); a deriver MUST
#: emit a null artifact for it and keep the row, never drop or shift it (R27 alignment contract).
Deriver = Callable[[pa.Table, list[bytes | None]], pa.Table]

_THUMBNAIL_COLUMN = "thumbnail"
_EMBEDDING_COLUMN = "embedding"

#: The columns a deriver may ADD to the carried table (absent upstream) — the single source of truth for
#: compute's columnLineage edge classification (#1): an output column matching one of these that is NOT in
#: the upstream schema was DERIVED from the blob content (TRANSFORMATION); everything else that survives a
#: stage is carried forward (IDENTITY). Extend alongside ``_DERIVERS`` when a new modality adds artifacts.
ARTIFACT_COLUMNS: tuple[str, ...] = (_THUMBNAIL_COLUMN, _EMBEDDING_COLUMN)


class UnderivableMediaError(ValueError):
    """A payload matched a deriver's probe but cannot actually be derived (e.g. a truncated image).

    DETERMINISTIC bad data — redelivery cannot fix bytes — so the mover routes this to the same
    DROP-with-FAIL-lineage path as an FGA denial / quality block, never the transient RETRY path
    (which would re-read every blob from S3 up to maxDeliver times for an identical failure).
    """


def _image_artifacts(table: pa.Table, payloads: list[bytes | None]) -> pa.Table:
    """The IMAGE deriver: inline ``thumbnail`` + ``embedding`` from the payloads; the original stays a blob.

    A payload past the probe that fails to decode (``is_image`` verifies headers, not full pixel data)
    raises :class:`UnderivableMediaError` naming the row — the run FAILs in lineage and the trigger is
    DROPPED as deterministic bad data.

    A NULL payload (the row's blob is null — a page the harvest skipped) derives NULL artifacts and keeps
    its row: absent bytes are not bad bytes, so failing the whole stage over one would be wrong, and
    dropping the row would break the positional contract every other column depends on.
    """
    thumbnails: list[bytes | None] = []
    embeddings: list[list[float] | None] = []
    for index, payload in enumerate(payloads):
        if payload is None:
            thumbnails.append(None)
            embeddings.append(None)
            continue
        try:
            thumbnails.append(media.derive_thumbnail(payload))
            embeddings.append(media.derive_embedding(payload))
        except Exception as exc:
            raise UnderivableMediaError(f"blob payload at row {index} matched the image probe but failed to decode: {exc}") from exc
    table = table.append_column(
        pa.field(_THUMBNAIL_COLUMN, pa.large_binary()),
        pa.array(thumbnails, pa.large_binary()),
    )
    return table.append_column(
        pa.field(_EMBEDDING_COLUMN, pa.list_(pa.float32(), media.EMBEDDING_DIMS)),
        pa.array(embeddings, type=pa.list_(pa.float32(), media.EMBEDDING_DIMS)),
    )


#: (content probe, deriver) — checked in order against the first payload of a blob column. Adding a
#: media type = one probe + one deriver here; the platform and the chart do not change.
_DERIVERS: tuple[tuple[Callable[[bytes], bool], Deriver], ...] = ((media.is_image, _image_artifacts),)


def is_derivable(payload: bytes) -> bool:
    """Whether ANY registered deriver would claim this payload.

    Public because the caller needs to answer "are these bytes worth reading at all" BEFORE reading
    them. `derive_artifacts` asks the same question of the same `_DERIVERS` table, but only after its
    caller has already materialised every payload to hand it one — which is the whole cost when the
    answer is no, and it is no for tabular, audio-until-a-deriver-exists, and anything unrecognised.

    Kept here rather than exported as `_DERIVERS` so the dispatch table stays private to the module
    that owns it: a new modality adds a row, and no caller has to learn the shape.
    """
    return any(probe(payload) for probe, _ in _DERIVERS)


def derive_artifacts(table: pa.Table, blob_payloads: dict[str, list[bytes | None]]) -> pa.Table:
    """Derive whatever the stage's blob content supports; pass everything else through untouched.

    Skips when the upstream already carries the artifact columns (a later stage carries them forward
    rather than re-deriving) and when there is nothing to probe (tabular / zero-row / unrecognised
    content). Dispatch keys on the first NON-NULL payload of the FIRST blob column (sorted —
    deterministic): a content-HOMOGENEOUS blob column is the contract; a mixed column derives-or-passes by
    that payload, and a matched-then-undecodable payload raises :class:`UnderivableMediaError` (→ DROP).

    Probing the first NON-null payload matters: a page whose harvest failed is a null blob (R27), and
    keying dispatch on ``payloads[0]`` would make a whole column's derivation depend on whether row 0
    happened to land.
    """
    if not blob_payloads:
        return table
    if _THUMBNAIL_COLUMN in table.column_names or _EMBEDDING_COLUMN in table.column_names:
        return table
    payloads = blob_payloads[min(blob_payloads)]
    first = next((payload for payload in payloads if payload is not None), None)
    if first is None:  # empty column, or every payload null — nothing to dispatch on
        return table
    for probe, deriver in _DERIVERS:
        if probe(first):
            return deriver(table, payloads)
    return table
