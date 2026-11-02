"""The lineage lane's published contract: rask's own OpenLineage facets, the uuids a dataset carries in them, and the
identity of an event.

What every reader of the lakehouse's lineage events needs, whoever it is: lineage's durable feed, the catalog's outbox,
and a client that targets people off the events (a notifier). The lakehouse's emitters build their events with
`lakehouse_kit.openlineage`, which builds on these names, so a reader and a writer cannot disagree about a facet key, a
uuid's place or an event's identity, and a client reads the events with no import from the lakehouse's kernel.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from pydantic import BaseModel, ConfigDict


#: Where rask publishes its custom facets' schemas (`spec/facets/rask/`): under a git tag, since OpenLineage asks a
#: facet's ``_schemaURL`` to be "an immutable pointer to the version of the facet schema", a tag or a sha, never a
#: branch. A changed schema is a new tag. ``lineage_kit.signing.SIGNATURE_FACET_SCHEMA_URL`` names the same tag.
RASK_FACET_SCHEMAS: Final = "https://raw.githubusercontent.com/AI-Riksarkivet/rask/facets-1.0.0/spec/facets/rask"
#: The ``_schemaURL`` of :data:`LANCE_FACET`, a JSON pointer to its definition (`RaskLanceFacet.json`).
LANCE_FACET_SCHEMA_URL: Final = f"{RASK_FACET_SCHEMAS}/RaskLanceFacet.json#/$defs/RaskLanceFacet"
#: The ``_schemaURL`` of :data:`AUTHOR_FACET` (`RaskAuthorFacet.json`).
AUTHOR_FACET_SCHEMA_URL: Final = f"{RASK_FACET_SCHEMAS}/RaskAuthorFacet.json#/$defs/RaskAuthorFacet"

#: rask's own dataset and run facet: the ref an input was read on or a run wrote, and the uuids the catalog minted for the
#: dataset's table and its namespace. Custom, so its key is ``{prefix}_{name}`` (OpenLineage spec, "Custom Facet Naming")
#: and its schema is rask's own (:data:`LANCE_FACET_SCHEMA_URL`, `lakehouse_kit.openlineage.lance_facet`).
LANCE_FACET: Final = "rask_lance"
#: rask's run facet (a DatasetEvent's dataset facet) naming the verified subject a run was authored by, ``{"name", "sub"}``.
#: Custom, named as :data:`LANCE_FACET` is, with its own schema (:data:`AUTHOR_FACET_SCHEMA_URL`,
#: `lakehouse_kit.openlineage.author_facet`).
AUTHOR_FACET: Final = "rask_author"
_TABLE_UUID: Final = "tableUuid"
_NAMESPACE_UUID: Final = "namespaceUuid"


class DatasetUuids(BaseModel):
    """The uuids the catalog minted for a dataset's table and that table's namespace (ADR 0173 §10).

    A dataset's identity across a rename is its table's uuid, not its name: a name can be given up and taken by another
    table, a uuid cannot. ``None`` where the dataset has none, an external source's always, or where it was not resolved.
    """

    model_config = ConfigDict(frozen=True)

    table_uuid: str | None = None
    namespace_uuid: str | None = None


def dataset_uuids(dataset: object) -> DatasetUuids:
    """The uuids an OpenLineage dataset's ``rask_lance`` facet carries, reading only the plain ``facets`` slot.

    Tolerant of a payload that is not the shape it should be, since every reader holds an event it did not build: a value
    that is not a non-empty string is no uuid.
    """
    facets = dataset.get("facets") if isinstance(dataset, dict) else None
    lance = facets.get(LANCE_FACET) if isinstance(facets, dict) else None
    lance = lance if isinstance(lance, dict) else {}

    def text(key: str) -> str | None:
        value = lance.get(key)
        return value if isinstance(value, str) and value else None

    return DatasetUuids(table_uuid=text(_TABLE_UUID), namespace_uuid=text(_NAMESPACE_UUID))


def with_dataset_uuids(dataset: dict[str, Any], uuids: DatasetUuids, *, producer: str) -> dict[str, Any]:
    """``dataset`` with its ``rask_lance`` facet naming exactly ``uuids``: a uuid set where it has one, and removed where it has none.

    Removed rather than kept, so a stamp is the whole answer: a dataset carrying a uuid the stamping service did not
    resolve would keep a claim nobody checked. The ``rask_lance`` facet's other fields (an input's ``ref``) stay, and its
    ``_producer`` and ``_schemaURL`` become ``producer``'s and rask's schema, since the stamped facet is rask's to describe
    whatever a client's said ([[LH-500]]). The facet is left out when it would hold nothing but those two. ``dataset`` itself
    is not changed.
    """
    facets = dict(dataset.get("facets") or {})
    lance = dict(facets.get(LANCE_FACET) or {})
    for key, value in ((_TABLE_UUID, uuids.table_uuid), (_NAMESPACE_UUID, uuids.namespace_uuid)):
        if value:
            lance[key] = value
        else:
            lance.pop(key, None)
    if set(lance) - {"_producer", "_schemaURL"}:
        facets[LANCE_FACET] = {**lance, "_producer": producer, "_schemaURL": LANCE_FACET_SCHEMA_URL}
    else:
        facets.pop(LANCE_FACET, None)
    stamped = {**dataset, "facets": facets}
    if not facets:
        stamped.pop("facets")
    return stamped


#: The UUID namespace for a static metadata event's derived identity. A fixed literal rather than a
#: hash of anything, because it must be byte-identical in every service that derives one — a namespace
#: that varies is two namespaces.
_STATIC_EVENT_NAMESPACE: Final = uuid.UUID("6f0d9bb2-3c47-5f1a-9b8e-4a2d1c7e5f30")


def static_event_id(*, producer: str, namespace: str, name: str, operation: str, event_time: str) -> str:
    """The identity of a ``DatasetEvent`` — derived from its content, never minted.

    A static metadata event carries no run id, and two things downstream need one anyway: the durable
    feed keys a static change's row on this id (``lineage.services.provenance.feed_key``), so a minted id
    would land the row again on every at-least-once redelivery; and a notifier keys what it delivers by an
    event's identity and state, so a minted id would tell one change twice.

    DERIVED HERE BECAUSE IT HAS TWO INDEPENDENT READERS SEEING DIFFERENT COPIES. A notifier reading the
    bus reads the catalog's raw publish, while one walking the feed reads what lineage stored, and the two
    must agree exactly or one change lands twice. A shared function is the only thing that makes that
    true by construction rather than by two implementations happening to match.

    ``event_time`` IS PART OF THE IDENTITY, and both directions matter: a redelivery carries the same
    instant and so collapses onto one id, while a genuine repeat — a table dropped and created again —
    carries a different one and stays a second fact rather than being silently swallowed by the first.
    """
    return str(uuid.uuid5(_STATIC_EVENT_NAMESPACE, "|".join((producer, namespace, name, operation, event_time))))


def event_identity(event: dict[str, Any]) -> str:
    """The identity of one OpenLineage event on the wire, whichever shape it is.

    A run event IS its run id. A ``DatasetEvent`` has none, and three hops need one anyway — the
    catalog's outbox stages under it (a wrong key stages under a name the relay cannot find, which is
    the provenance loss the outbox exists to prevent), lineage's durable feed dedups on it, and a
    notifier keys what it delivers by it. One derivation, so a single change cannot acquire three
    identities and be staged, fed and delivered as three different facts.
    """
    run = event.get("run")
    if isinstance(run, dict) and run.get("runId"):
        return str(run["runId"])
    dataset = event.get("dataset")
    dataset = dataset if isinstance(dataset, dict) else {}
    facets = dataset.get("facets")
    facets = facets if isinstance(facets, dict) else {}
    lance = facets.get(LANCE_FACET)
    lance = lance if isinstance(lance, dict) else {}
    return static_event_id(
        producer=str(event.get("producer") or ""),
        namespace=str(dataset.get("namespace") or ""),
        name=str(dataset.get("name") or ""),
        operation=str(lance.get("operation") or ""),
        event_time=str(event.get("eventTime") or ""),
    )
