"""Response models for the diarization endpoint (``/api/diarization``)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SpeakerTurn(BaseModel):
    """One diarized speaker turn in absolute video seconds.

    ``speaker`` is pyannote's per-video local label (``SPEAKER_00`` …), stable
    only within a single video — never compare it across docs.
    """

    turn_id: int
    speaker: str
    start: float
    end: float


class DiarizationResponse(BaseModel):
    """A video's speaker turns (sorted by start) + its sorted-distinct speakers.

    ``built`` is ``False`` with empty ``turns``/``speakers`` when the diarization
    table or this doc's rows are absent — the timeline then renders nothing.
    """

    built: bool
    doc_id: str
    turns: list[SpeakerTurn] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
