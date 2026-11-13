"""The CloudEvents envelope each of the lakehouse's events rides the bus in (CloudEvents 1.0, structured JSON mode).

Every attribute is read off the event, so one event has one envelope whoever publishes it: the producer that made it, or
a relay delivering its staged copy. The bus needs that of the ``id``. A JetStream stream drops a message whose
``Nats-Msg-Id`` it has seen inside its duplicate window and still acknowledges the publish, and the message id is this
``id``: two events sharing one lose the later, and one event under two ids is stored twice. A run's START and its
COMPLETE share a run id, so the id is the event's identity and its state together.

``data`` is the event exactly as its producer signed it: a signature covers the event, never its envelope
(`lakehouse.lineage.signing`). The contract a reader of the bus holds is `docs/COMMIT-EVENTS.md` § The envelope on the
bus.

A model of the lakehouse's own rather than the CloudEvents Python SDK: that SDK (2.2.0, read 2026-11-13) has no pydantic
model and no NATS binding, and what is sent here is a handful of attributes and the event.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from lakehouse.control_events import CatalogControlEvent, control_signer_role
from lakehouse.lineage.schemas import TERMINAL_STATES
from lakehouse.lineage_events import dataset_uuids, event_identity


#: The content type of a structured-mode CloudEvent in the JSON format: the whole envelope is the message body.
STRUCTURED_CONTENT_TYPE: Final = "application/cloudevents+json"

_SPEC_VERSION: Final = "1.0"
_DATA_CONTENT_TYPE: Final = "application/json"

#: The ``source`` of an event that names no OpenLineage ``producer``. Every lakehouse emitter names its own; the
#: attribute is required, and a relay must be able to deliver whatever its feed recorded.
_LINEAGE_SOURCE: Final = "rask/lineage"
_LINEAGE_DATASET_TYPE: Final = "rask.lineage.dataset.v1"
#: The state a dataset change is identified under. It has happened by the time it is announced, and lineage's feed keeps
#: its row under this state (`docs/COMMIT-EVENTS.md` § A dataset change).
_DATASET_STATE: Final = "COMPLETE"
#: OpenLineage's own state for a run event that declares no other transition.
_UNDECLARED_STATE: Final = "OTHER"


class CloudEvent(BaseModel):
    """One event in its envelope: the CloudEvents 1.0 context attributes the lakehouse sets, and the event as ``data``.

    Attributes:
        id: The event's identity on the bus, the same for every copy of it and for no other event.
        source: Who produced the event.
        type: What kind of event it is, versioned.
        subject: The one object the event is about, where it is about exactly one.
        time: When what the event tells happened.
        dataschema: The schema ``data`` follows, where the event names one.
        data: The event as its producer signed it.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    type: str = Field(min_length=1)
    subject: str | None = Field(default=None, min_length=1)
    time: AwareDatetime | None = None
    dataschema: str | None = Field(default=None, min_length=1)
    data: dict[str, Any]

    def as_wire(self) -> dict[str, Any]:
        """The JSON object the bus carries: each attribute that is set, and ``data`` as it was given.

        ``data`` is handed on, never dumped: it is its producer's document, and a JSON-mode dump rewrites what JSON
        cannot say (measured on pydantic 2.13.4: a non-finite number inside it becomes null).
        """
        attributes = self.model_dump(mode="json", exclude={"data"}, exclude_none=True)
        return {"specversion": _SPEC_VERSION, "datacontenttype": _DATA_CONTENT_TYPE, **attributes, "data": self.data}


def lineage_envelope(event: Mapping[str, Any]) -> CloudEvent:
    """The envelope of one OpenLineage event, a run's or a dataset change's, on the lineage topic.

    Total over every event lineage's doors parse, since its relay delivers whatever a producer staged.

    Args:
        event: The event as its producer signed it.

    Returns:
        The event under its identity and state (the key lineage's feed keeps a run's row under), from its ``producer``,
        about the one governed table it writes where it writes exactly one.
    """
    return CloudEvent(
        id=_lineage_id(event),
        source=_text(event.get("producer")) or _LINEAGE_SOURCE,
        type=_lineage_type(event),
        subject=_written_table(event),
        time=_instant(event.get("eventTime")),
        dataschema=_text(event.get("schemaURL")),
        data=dict(event),
    )


def control_envelope(event: Mapping[str, Any]) -> CloudEvent:
    """The envelope of one control event on the control topic.

    Args:
        event: The event as the bus carries it, with the signature its emitter attached.

    Returns:
        The event under its ``event_id``, from the role that signs its action, about the object it names.

    Raises:
        pydantic.ValidationError: ``event`` is no control event, or one no envelope can carry (an empty ``event_id``).
    """
    parsed = CatalogControlEvent.model_validate(event)
    return CloudEvent(
        id=parsed.event_id,
        source=f"rask/{control_signer_role(parsed.action)}",
        type=f"rask.catalog.control.{parsed.action}.v1",
        subject=parsed.object_id or None,
        time=_in_utc(parsed.occurred_at),
        data=dict(event),
    )


def _is_dataset_change(event: Mapping[str, Any]) -> bool:
    """Whether lineage's doors read ``event`` as a dataset change rather than a run's event (`lineage.models.parse_event`)."""
    return "dataset" in event and "run" not in event


def _run_state(event: Mapping[str, Any]) -> str:
    """The state a run's event declares, read once for its id and its type alike."""
    return str(event.get("eventType") or _UNDECLARED_STATE)


def _lineage_id(event: Mapping[str, Any]) -> str:
    identity = event_identity(event)
    if _is_dataset_change(event):
        return f"{identity}@{_DATASET_STATE}"
    state = _run_state(event)
    if state in TERMINAL_STATES:
        return f"{identity}@{state}"
    # A state that ends nothing can be told again as the run goes on, so each telling is its own event.
    return f"{identity}@{state}@{event.get('eventTime') or ''}"


def _lineage_type(event: Mapping[str, Any]) -> str:
    if _is_dataset_change(event):
        return _LINEAGE_DATASET_TYPE
    return f"rask.lineage.run.{_run_state(event).lower()}.v1"


def _written_table(event: Mapping[str, Any]) -> str | None:
    """The one governed table ``event`` writes or changes, as the object its grants are held by (``table:<uuid>``, ADR
    0173 §8); None when it names several tables, none, or one the catalog named by no uuid."""
    datasets = [event.get("dataset")] if _is_dataset_change(event) else event.get("outputs")
    if not isinstance(datasets, list) or len(datasets) != 1:
        return None
    uuid = dataset_uuids(datasets[0]).table_uuid
    return f"table:{uuid}" if uuid else None


def _instant(event_time: object) -> datetime | None:
    """The instant an OpenLineage ``eventTime`` names, or None for one that names none: a string no time parses from, or
    a time with no offset, which is no point in time."""
    try:
        parsed = datetime.fromisoformat(str(event_time))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _in_utc(occurred_at: datetime) -> datetime:
    """A control event's time as an instant: one written with no offset is UTC, as `CatalogControlEvent.occurred_at` says."""
    return occurred_at if occurred_at.tzinfo is not None else occurred_at.replace(tzinfo=UTC)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
