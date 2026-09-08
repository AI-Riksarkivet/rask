"""An event the door refused must reach somebody who can save it — without failing the run.

§ E1's second half. `Emitter.emit()` already answers a bool ("this event needs no recovery"), and the
emitter is the only thing that knows; `LineageRun` then discards it, so no caller can decide to stage.
That is the whole gap: four of the five lakehouse producers stage durably through the shared
object-store outbox, and `ingest` has zero outbox usage and emits bare.

IT HAS ALREADY HAPPENED TWICE ON THAT LANE, and the estate wrote it down in
`ingest/service_identity.py`: "a 401 there does not surface as an error, it surfaces as a permanent gap
in the graph that looks exactly like a healthy estate. That has already happened twice on this lane
(the trainer in 2026-07, `service-ingest` on 2026-08-06, a day of 403s while the data landed)."

THE HOOK IS INJECTED, NOT BUILT IN, for a reason the register records: the other four producers ALREADY
stage, so a recorder that staged on their behalf would double-stage and grow an outbox no relay can
distinguish from real backlog. Only the producer knows whether it has its own durability.

AND IT MUST NEVER RAISE. I8: a run whose data landed must not be reported as failed because the graph
was unreachable — that would turn an observability outage into a data incident. A hook that throws is
the same failure wearing a different hat, so the recorder contains it.
"""

from __future__ import annotations

from typing import Any, cast

from lineage_kit.emitter import Emitter
from lineage_kit.runs import LineageRun


class _RefusingEmitter:
    """The measured shape: the door answers 401/403, the emitter logs, counts, and reports False."""

    def emit(self, event: Any) -> bool:
        return False


class _AcceptingEmitter:
    def emit(self, event: Any) -> bool:
        return True


def _run(emitter: Any, **kw: Any) -> LineageRun:
    return LineageRun(job_name="ingest.run", namespace="ingest", emitter=cast(Emitter, emitter), **kw)


def test_a_refused_event_is_handed_to_the_hook() -> None:
    """The event itself, not a notification — the hook has to be able to stage the exact bytes."""
    saved: list[Any] = []
    run = _run(_RefusingEmitter(), on_undelivered=saved.append)
    event = run.start()

    assert saved, "the door refused and nothing was handed over — the event is gone and the graph shows a healthy estate"
    assert saved[0] is event, "the hook received something other than the event it must stage"


def test_a_delivered_event_is_NOT_handed_over() -> None:
    """Staging a delivered event grows an outbox nothing drains, which a relay cannot tell from real
    backlog — the same reason `NoopEmitter` answers True."""
    saved: list[Any] = []
    _run(_AcceptingEmitter(), on_undelivered=saved.append).start()
    assert not saved, "a delivered event was staged anyway"


def test_every_event_the_run_emits_is_covered() -> None:
    """START and terminal alike. A recorder that recovered only its START would leave the graph holding
    runs that never end, which reads as a hung cascade rather than as a lost event."""
    saved: list[Any] = []
    run = _run(_RefusingEmitter(), on_undelivered=saved.append)
    run.start()
    run.complete()
    assert len(saved) >= 2, f"only {len(saved)} of the run's events were offered for recovery"


def test_a_THROWING_hook_never_fails_the_run() -> None:
    """I8, restated where it can actually break: the recovery path is still observability. A stager that
    cannot reach the object store must not turn a landed run into a failed one."""

    def _explode(event: Any) -> None:
        raise RuntimeError("outbox unreachable")

    run = _run(_RefusingEmitter(), on_undelivered=_explode)
    run.start()  # must not raise


def test_no_hook_is_the_previous_behaviour() -> None:
    """Every producer that already stages, and every one that has opted out of lineage, is untouched."""
    _run(_RefusingEmitter()).start()  # must not raise
