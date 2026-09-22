"""Whether a dataset's recorded location can ever be resolved, and who to tell when it cannot.

ONE IMPLEMENTATION OF THE PREDICATE, imported by both sides. The sweep has refused a relative
``dataSource`` URI since it was written — correctly, because "a relative path cannot say whether the
data is there" — but only at READ time, and only as a count. So the estate stored exactly what it
would later refuse, every tick, forever, and the refusal named the DATASET rather than the producer
that emitted the facet ([[LH-187]]).

Attribution belongs at INGEST because that is the only moment the producer is still in hand. By the
time the sweep reads the graph, the event is gone and nothing links the unresolvable URI back to
whoever wrote it.

THIS MODULE DOES NOT DECIDE WHETHER TO REFUSE. Whether an event carrying an unresolvable URI should
be rejected outright is a policy question with a real cost — ``dataSource`` is an OPTIONAL
OpenLineage facet and external producers emit it, so refusing costs a third party a whole run event
over one field. Reporting is the half that is right under either answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel


if TYPE_CHECKING:
    from lineage.models import RunEvent


def names_a_storage_location(uri: str) -> bool:
    """Whether ``uri`` says where the data is, rather than only what it is called.

    An absolute path or any scheme-qualified URI answers the question; a bare name does not — it is
    resolvable only against a root this side does not know, so an open would report "not found" for a
    dataset that may be perfectly healthy.
    """
    return uri.startswith("/") or "://" in uri


class UnresolvableSource(BaseModel):
    """One dataset whose recorded location can never be resolved, and the producer that said so.

    FROZEN because it is a value object that crosses a boundary: it is built here and read by a log
    call, and nothing downstream has any business editing the attribution.
    """

    model_config = {"frozen": True}

    dataset: str
    uri: str
    producer: str


def unresolvable_sources(event: RunEvent) -> list[UnresolvableSource]:
    """Every dataset on ``event`` carrying a ``dataSource`` URI that names no storage location.

    Reads the FACET's own ``_producer`` before the event-level one: a producer that emitted a
    malformed facet is more specific than the tool that sent the envelope, and for a relayed event
    (the outbox drain re-ingests) the two differ. Empty when the event is clean, which is the normal
    case and costs one string check per dataset.
    """
    found: list[UnresolvableSource] = []
    for dataset in (*event.inputs, *event.outputs):
        uri = dataset.source_uri
        if uri is None or names_a_storage_location(uri):
            continue
        facet = dataset.facet("dataSource") or {}
        producer = str(facet.get("_producer") or event.producer or "<unnamed>")
        found.append(UnresolvableSource(dataset=f"{dataset.namespace}${dataset.name}", uri=uri, producer=producer))
    return found
