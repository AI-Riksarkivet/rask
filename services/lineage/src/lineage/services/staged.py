"""A staged outbox object read back and re-announced — ONE copy of each step for every reader.

The reconcile relay (`api/reconcile_cron._drain_outbox`) and the DLQ view and replay
(`api/v1/endpoints/dlq`) read the same staged bytes, and the relay and the replay both hand a recovered
event on to the bus. Separate copies would let one path drop as poison what another ingests, or tell
subscribers what another withholds, unseen.
"""

from __future__ import annotations

import json

from lineage.core.config import LineageSettings
from lineage.models import DatasetEvent, RunEvent, parse_event
from service_kit import dapr_publish


class UnparseableEventError(ValueError):
    """Staged bytes that are no OpenLineage event: deterministic, so no retry can repair them."""


def parse_staged(event_json: str) -> RunEvent | DatasetEvent:
    """The staged object as whichever event it is, through the doors' own discriminator (`parse_event`).

    Raises:
        UnparseableEventError: for ANY bytes that do not parse, and for nothing else. `json.loads`
            refuses hostile input with more than `JSONDecodeError` — an integer past 4300 digits is a plain
            `ValueError`, deep nesting a `RecursionError` — and one such object, read oldest-first, would
            otherwise abort every drain tick and 500 both DLQ routes. The parse does no I/O, so no
            transient failure can be mistaken for poison here.
    """
    try:
        return parse_event(json.loads(event_json))
    except (ValueError, RecursionError) as exc:  # `JSONDecodeError` and pydantic's `ValidationError` are both `ValueError`s
        raise UnparseableEventError(str(exc)) from exc


async def republish_staged(publisher: object | None, settings: LineageSettings, event_json: str) -> None:
    """Re-announce a recovered event on the lineage topic, so its SUBSCRIBERS act on it as well as the graph.

    Ingesting alone repairs the graph and leaves its subscribers unaware: a recovered head RUN never
    reaches medallion's `/bronze-arrival`, so the cascade it should start never runs, and a recovered DDL
    change never reaches the notifications bus lane. ``None`` is a deployment without the outbox, which has
    nothing to re-announce (`api.dependencies.get_publisher`).

    THE STAGED BYTES, never `event.model_dump_json()`. The model is the parsed Python shape (`run_id`,
    `event_type`); the wire is OpenLineage (`runId`, `eventType`), so a round-trip through the model
    publishes a document no subscriber can parse. Byte-identical redelivery is also what subscribers would
    have seen the first time.
    """
    if publisher is None:
        return
    await dapr_publish.publish_event(
        publisher,
        timeout_seconds=settings.dapr_publish_timeout_seconds,
        pubsub_name=settings.dapr_pubsub,
        topic_name=settings.dapr_topic,
        data=event_json,
        data_content_type="application/json",
    )
