"""A staged outbox object read back and re-announced — ONE copy of each step for every reader.

The relay tick (`api/relay._drain_outbox`) and the DLQ view and replay
(`api/v1/endpoints/dlq`) read the same staged bytes, and the relay and the replay both hand a recovered
event on to the bus. Separate copies would let one path drop as poison what another ingests, or tell
subscribers what another withholds, unseen.
"""

from __future__ import annotations

from lakehouse.events.bus import Bus, publish_event  # noqa: TID251 — the relay's republish is a registered publish site
from lakehouse.events.cloud_events import CloudEvent, lineage_envelope
from lakehouse.events.outbox import staged_envelope
from lineage.core.config import LineageDelivery
from lineage.models import DatasetEvent, RunEvent, parse_event


class UnparseableEventError(ValueError):
    """Staged bytes that are no OpenLineage event: deterministic, so no retry can repair them."""


def read_staged(record: str) -> CloudEvent:
    """The envelope a staged object holds: the event as ``data``, and the trace it was staged under (ADR 0209).

    Raises:
        UnparseableEventError: for ANY bytes that hold no envelope, and for nothing else. Every such body is refused
            by the envelope's own parse as a `ValidationError` (measured 2026-11-18 on the locked pydantic: an integer
            past the parser's range and a document nested 100,000 deep alike), and one such object, read oldest-first,
            would otherwise abort every drain tick and 500 both DLQ routes. The read does no I/O, so no transient
            failure can be mistaken for poison here.
    """
    try:
        return staged_envelope(record)
    except ValueError as exc:  # pydantic's `ValidationError` is a `ValueError`
        raise UnparseableEventError(str(exc)) from exc


def parse_staged(staged: CloudEvent) -> RunEvent | DatasetEvent:
    """The staged event as whichever event it is, through the doors' own discriminator (`parse_event`).

    Raises:
        UnparseableEventError: the envelope's ``data`` is no OpenLineage event.
    """
    try:
        return parse_event(staged.data)
    except (ValueError, RecursionError) as exc:
        raise UnparseableEventError(str(exc)) from exc


async def republish_staged(publisher: Bus | None, settings: LineageDelivery, staged: CloudEvent) -> None:
    """Re-announce a recovered event on the lineage topic, so its SUBSCRIBERS act on it as well as the feed.

    Recording alone repairs the feed and leaves its subscribers unaware: a recovered RUN never reaches
    maintenance's arrival consumer or a notifier on the bus, and a recovered DDL change never reaches a
    notifier's bus lane. ``None`` is a deployment without the outbox, which has
    nothing to re-announce (`api.dependencies.get_publisher`).

    ``staged`` IS THE STAGED ENVELOPE AS IT PARSES, never the model's dump. The model is the parsed Python shape
    (`run_id`, `event_type`); the wire is OpenLineage (`runId`, `eventType`), so a round-trip through the model
    publishes a document no subscriber can parse. The staged event is what subscribers would have seen the
    first time, and its envelope is derived from it alone (`lakehouse.events.cloud_events.lineage_envelope`), so the
    copy goes out under the id its producer's publish carried: inside its duplicate window the stream stores
    it only when that publish never arrived. It goes out in the trace it was staged under (ADR 0209).
    """
    if publisher is None:
        return
    await publish_event(
        publisher, lineage_envelope(staged.data).with_trace_of(staged), topic=settings.lineage_topic, timeout_seconds=settings.publish_timeout_seconds
    )
