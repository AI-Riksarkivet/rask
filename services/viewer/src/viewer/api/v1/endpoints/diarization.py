"""Diarization endpoint — serve a recording's speaker turns for the player timeline.

Port of the old ``backend/diarization/router.py``: the turns table name now
comes from the descriptor's ``diarization`` capability, the dataset from the
optional ``dataset`` query param, and the ``doc_id`` whitelist from the
descriptor's ``identity.doc_key_pattern``. The table is read on demand per
request (never cached at startup) so a diarization rebuild is picked up
without restarting the server — and an undeclared capability, absent table,
or unknown doc simply returns ``built: false``.

No ``from __future__ import annotations`` here: FastAPI introspects these
signatures at runtime, so the annotations stay real objects.
"""

import re

import lance
from fastapi import APIRouter

from service_kit.exceptions import ValidationError
from service_kit.lancekit import store
from service_kit.lancekit.predicate import eq
from service_kit.media.deps import StateDep
from service_kit.media.state import dataset_handle
from viewer.schemas.diarization import DiarizationResponse, SpeakerTurn


router = APIRouter(prefix="/api/diarization", tags=["diarization"])

# The turns table's column names are the diarization capability's
# writer-guaranteed contract (the diarize writer always produces exactly
# these, non-null), NOT corpus schema — so they stay module constants.
_TURN_DOC = "doc_id"
_TURN_ID = "turn_id"
_TURN_SPEAKER = "speaker_label"
_TURN_START = "start"
_TURN_END = "end"


def _not_built(doc_id: str) -> DiarizationResponse:
    """The empty contract payload for an undeclared capability / missing table / doc."""
    return DiarizationResponse(built=False, doc_id=doc_id)


@router.get("/{doc_id}")
def get_diarization(doc_id: str, state: StateDep, dataset: str | None = None) -> DiarizationResponse:
    """A recording's speaker turns (sorted by start), or ``built: false`` if absent."""
    handle = dataset_handle(state, dataset)
    # Whitelist the path param against the descriptor's identity pattern BEFORE
    # it is inlined into the Lance filter literal below — otherwise a crafted
    # doc_id is a SQL-injection vector.
    if re.fullmatch(handle.descriptor.declared.identity.doc_key_pattern, doc_id) is None:
        raise ValidationError("invalid doc_id")
    target = handle.descriptor.declared.capabilities.get("diarization")
    if target is None:
        return _not_built(doc_id)
    table = target.partition(".")[0]
    uri = handle.table_uri(table)
    if not store.exists(uri, handle.storage_options):
        return _not_built(doc_id)
    rows = lance.dataset(uri, storage_options=handle.storage_options).to_table(filter=eq(_TURN_DOC, doc_id)).to_pylist()
    if not rows:
        return _not_built(doc_id)
    # The columns are a writer-guaranteed contract (every row carries all five,
    # non-null), so index directly — a schema drift should 500 loudly, not
    # silently return a built-but-empty payload the timeline can't render.
    turns = sorted(
        (
            SpeakerTurn(
                turn_id=int(row[_TURN_ID]),
                speaker=row[_TURN_SPEAKER],
                start=float(row[_TURN_START]),
                end=float(row[_TURN_END]),
            )
            for row in rows
        ),
        key=lambda turn: turn.start,
    )
    speakers = sorted({turn.speaker for turn in turns})
    return DiarizationResponse(built=True, doc_id=doc_id, turns=turns, speakers=speakers)
