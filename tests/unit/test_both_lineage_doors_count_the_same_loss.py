"""A refused ingest is counted the same whichever door it arrives at.

TWO DOORS REACH ONE GRAPH and only one of them was counting its losses. The Dapr subscriber
classifies every refusal into `Outcome` — `REFUSED` for a denied grant, `UNREPAIRABLE` for a run no
tuple or redelivery could ever fix — and `test_a_lineage_outcome_that_LOSES_a_run_is_audible` makes
each of those alertable. The HTTP ingest door recorded `INGESTED` on success and nothing at all on
refusal.

THE ASYMMETRY IS NOT COSMETIC, because the HTTP door is the ONLY door for a producer with no Dapr
sidecar: the whole Ray lane, every runner, and any external OpenLineage producer. Measured live
2026-09-21, a dummy-lane job wrote 64 rows to `acme-silver$dummy` and its provenance was refused
`ingest_denied … relation='can_write_data'`; the write is governed, the run is absent from the graph,
and the job exited SUCCEEDED ([[LH-184]]).

THE COUNT IS NOT REDUNDANT WITH THE HTTP STATUS CODE, which is what the old reasoning assumed by
treating a 403 as a "transport-level failure" the generic RED metrics already cover. This service
answers 403 for ordinary reads too — a `can_get_metadata` denial on a GET is one, measured in the same
log — so an undifferentiated status-code count cannot separate a reader being told no from a
producer's provenance being dropped. `Outcome.REFUSED` exists precisely because, as its own docstring
says, a refusal "is the one that can be a silent, deliberate loss, so it gets its own alert".

DRIVEN THROUGH THE ENDPOINT rather than compared as source text: the property is that the two doors
answer the same exception with the same outcome, and only running them shows that.
"""

from __future__ import annotations

from typing import Any

import pytest
from lance_namespace import PermissionDeniedError

from lineage.core.metrics import Outcome
from lineage.models import UnauthoredRunError, UngovernedOutputError


#: The subscriber's classification, which is the REFERENCE this door must match
#: (`services/lineage/src/lineage/services/consumer.py:101-120`).
SUBSCRIBER_CLASSIFICATION = [
    (PermissionDeniedError("can_write_data required on outputs: acme-silver$dummy"), Outcome.REFUSED),
    (UngovernedOutputError("can_write_data required on outputs: acme-silver$dummy"), Outcome.UNREPAIRABLE),
    (UnauthoredRunError("the run carries no author"), Outcome.UNREPAIRABLE),
]


@pytest.mark.parametrize(("error", "expected"), SUBSCRIBER_CLASSIFICATION, ids=lambda v: type(v).__name__ if isinstance(v, Exception) else str(v))
@pytest.mark.asyncio
async def test_the_http_door_counts_a_refusal_the_way_the_subscriber_does(error: Exception, expected: Outcome, monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.api.v1.endpoints import ingest

    recorded: list[Outcome] = []

    async def _deny(*_args: Any, **_kw: Any) -> None:
        raise error

    def _author(*_args: Any, **_kw: Any) -> None:
        if isinstance(error, UnauthoredRunError):
            raise error

    monkeypatch.setattr(ingest, "enforce_author", _author)
    monkeypatch.setattr(ingest, "enforce_output_authz", _deny)
    monkeypatch.setattr(ingest, "record_outcome", recorded.append)

    with pytest.raises(type(error)):
        await ingest.ingest_event(event=object(), request=object(), repository=object(), settings=object(), token=object())  # ty: ignore[invalid-argument-type]

    assert recorded == [expected], f"the HTTP door recorded {recorded or 'nothing'} for {type(error).__name__}; the subscriber records {expected.value}"


@pytest.mark.asyncio
async def test_a_SUCCESSFUL_ingest_still_counts_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard against paying for the above with a double count on the path that works."""
    from lineage.api.v1.endpoints import ingest

    recorded: list[Outcome] = []

    class _Run:
        run_id = "run-1"

    class _Event:
        run = _Run()

    class _Repo:
        async def ingest_event(self, _event: Any) -> None:
            return None

    monkeypatch.setattr(ingest, "enforce_author", lambda *_a, **_k: None)

    async def _allow(*_args: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(ingest, "enforce_output_authz", _allow)
    monkeypatch.setattr(ingest, "record_outcome", recorded.append)

    answer = await ingest.ingest_event(event=_Event(), request=object(), repository=_Repo(), settings=object(), token=object())  # ty: ignore[invalid-argument-type]

    assert recorded == [Outcome.INGESTED]
    assert answer["status"] == "ingested"
