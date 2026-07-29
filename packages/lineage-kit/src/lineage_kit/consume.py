"""The CONSUME-layer projection of a run event — provenance that travels WITH the data (R25b / R26).

The south side's goal is a Lance dataset an external user can take away: the governed catalog hands
them the data, and :class:`LineageDoc` is the provenance that rides *inside* it as a ``pa.json_()``
(JSONB) column, so answering "who made this row, from what, when" costs a column read — not a Cypher
query against Apache AGE.

**One provenance shape, not two.** A doc is never hand-authored: :meth:`LineageDoc.from_run_event`
PROJECTS the very :class:`~lineage_kit.schemas.RunEvent` its emitter also publishes to the lineage
service, so the JSONB and the graph are the same facts by construction. Everything the doc carries
beyond the event is *measured upstream state* the event does not claim (an input's on-disk Lance
version + URI) or *inherited* from the upstream dataset's own doc (the ``derived_from`` tail) — so
there is nothing for the two to disagree about.

**The chain.** ``derived_from`` is the ``DERIVED_FROM`` walk, newest hop first, terminating at the
root governed tier (bronze — R23: raw is the external world and owns no dataset). Each stage prepends
its own hop to the chain it read off its upstream's doc (:meth:`LineageDoc.inherited_chain`), so a
gold row names every hop back to bronze without a graph query and without any stage knowing the DAG.

**Queryability.** Lance stores ``pa.json_()`` as JSONB and exposes ``json_get_*`` / ``json_extract`` /
``json_exists`` / ``json_array_contains`` / ``json_array_length`` in FILTERS (not projections). Those
functions take a TOP-LEVEL key or an explicit path argument, so the fields a consumer is most likely
to filter on (``run_id``, ``operation``, ``author``, ``event_time``, ``event_type``) are flat scalars
at the document root; nested objects are reached by chaining ``json_get``. ``chain`` mirrors
``derived_from``'s dataset names as a flat string array precisely so
``json_array_contains(lineage, 'chain', 'bronze$events')`` answers "did this row come from bronze?" in
one predicate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Self

from pydantic import Field

from lineage_kit.schemas import Dataset, JobRef, RunEvent, _Model


#: The document's own version tag — bump when the shape changes incompatibly, so a consumer holding an
#: old gold dataset can tell which contract it is reading.
LINEAGE_DOC_SCHEMA = "rask.lineage.consume/1"

#: The run facet carrying the platform's ``{name, sub}`` identity (``services/lineage`` reads
#: ``run.facets.author.name``); mirrored to the doc's flat ``author``.
AUTHOR_RUN_FACET = "author"
#: The lakehouse run facet carrying ``{operation, version, token, project}``; ``operation`` is the
#: human-readable name of what the run did and is mirrored to the doc's flat ``operation``.
LANCE_RUN_FACET = "lance"


class DatasetRef(_Model):
    """A dataset a consumer can go and open: catalog identity plus, when known, physical coordinates."""

    namespace: str
    name: str
    #: The Lance dataset version this run READ (inputs) — a string, matching OpenLineage's
    #: ``DatasetVersionDatasetFacet.datasetVersion``. ``None`` where the producer did not measure it.
    version: str | None = None
    #: The physical URI (``s3://…``), from the standard ``dataSource`` facet when present.
    uri: str | None = None

    @classmethod
    def from_dataset(cls, dataset: Dataset) -> Self:
        """Project an event input/output dataset, lifting its ``version``/``dataSource`` facets."""
        version = dataset.facets.version
        source = dataset.facets.data_source
        return cls(
            namespace=dataset.namespace,
            name=dataset.name,
            version=version.dataset_version if version else None,
            uri=source.uri if source else None,
        )


class LineageEdge(_Model):
    """One ``DERIVED_FROM`` hop: ``downstream`` was produced FROM ``upstream`` by the named run.

    Self-contained on purpose — a consumer walking ``derived_from`` gets the who/when/what of every
    hop without dereferencing anything, which is the whole point of shipping provenance in-band.
    """

    downstream: DatasetRef
    upstream: DatasetRef
    run_id: str
    job: JobRef
    operation: str | None = None
    author: str | None = None
    event_time: str


class LineageDoc(_Model):
    """The provenance document written into a governed dataset's ``lineage`` JSONB column.

    Describes the run that produced THIS dataset version and the derivation chain behind it. It
    deliberately does NOT record its own output version: the value is written in the same commit that
    creates that version, so it cannot name it without lying. A consumer already holds the dataset —
    ``lance.dataset(uri).version`` is the answer — and ``run_id`` joins the row back to the graph.
    """

    doc_schema: str = Field(default=LINEAGE_DOC_SCHEMA, alias="schema")
    run_id: str
    job: JobRef
    operation: str | None = None
    author: str | None = None
    event_time: str
    event_type: str
    producer: str
    inputs: list[DatasetRef] = Field(default_factory=list)
    output: DatasetRef
    #: The ``DERIVED_FROM`` walk, THIS hop first, back to the root governed tier.
    derived_from: list[LineageEdge] = Field(default_factory=list)
    #: Every dataset name in ``derived_from``'s upstream side — flat, so a single
    #: ``json_array_contains(lineage, 'chain', '<name>')`` filter answers "descended from <name>?".
    chain: list[str] = Field(default_factory=list)

    @classmethod
    def from_run_event(
        cls,
        event: RunEvent,
        *,
        inputs: Sequence[DatasetRef] | None = None,
        parent_chain: Sequence[LineageEdge] = (),
    ) -> Self:
        """Project ``event`` into the consume-layer document.

        ``inputs`` REPLACES the event's own input refs with measured ones (an emitter that records bare
        ``{namespace, name}`` inputs still knows the upstream Lance version + URI it just read) — it
        must name the same datasets, which :meth:`is_consistent_with` re-checks. ``parent_chain`` is the
        upstream dataset's own ``derived_from``, read off its ``lineage`` column; this run's hop is
        prepended, so the chain grows one link per stage and reaches the root without a graph query.
        """
        if not event.outputs:
            raise ValueError("a lineage document needs the run's output dataset")
        # The output's OWN version is deliberately not lifted: the document is written in the commit that
        # mints that version, so any value here would be a guess. See the class docstring.
        produced = event.outputs[0]
        output = DatasetRef(
            namespace=produced.namespace,
            name=produced.name,
            uri=produced.facets.data_source.uri if produced.facets.data_source else None,
        )
        resolved = list(inputs) if inputs is not None else [DatasetRef.from_dataset(d) for d in event.inputs]
        facets = event.run.facets.model_extra or {}
        operation = _facet_value(facets.get(LANCE_RUN_FACET), "operation")
        author = _facet_value(facets.get(AUTHOR_RUN_FACET), "name")
        hops = [
            LineageEdge(
                downstream=output,
                upstream=upstream,
                run_id=event.run.run_id,
                job=JobRef(namespace=event.job.namespace, name=event.job.name),
                operation=operation,
                author=author,
                event_time=event.event_time,
            )
            for upstream in resolved
        ]
        derived_from = [*hops, *parent_chain]
        return cls(
            run_id=event.run.run_id,
            job=JobRef(namespace=event.job.namespace, name=event.job.name),
            operation=operation,
            author=author,
            event_time=event.event_time,
            event_type=str(event.event_type),
            producer=event.producer,
            inputs=resolved,
            output=output,
            derived_from=derived_from,
            chain=list(dict.fromkeys(edge.upstream.name for edge in derived_from)),
        )

    @classmethod
    def inherited_chain(cls, raw: str | bytes | None) -> list[LineageEdge]:
        """The ``derived_from`` chain carried by an upstream dataset's ``lineage`` cell, or ``[]``.

        Tolerant by design: an upstream written before this column existed (``None``), or one whose cell
        a foreign writer left unparseable, yields an empty chain — the stage still records its OWN hop,
        so provenance degrades in depth, never in truthfulness.
        """
        if raw is None:
            return []
        try:
            doc = cls.model_validate_json(raw)
        except ValueError:
            return []
        return doc.derived_from

    def is_consistent_with(self, event: RunEvent) -> bool:
        """Whether this doc still describes ``event`` — the pin that JSONB and graph cannot diverge."""
        if not event.outputs:
            return False
        facets = event.run.facets.model_extra or {}
        return (
            self.run_id == event.run.run_id
            and self.job == JobRef(namespace=event.job.namespace, name=event.job.name)
            and self.event_time == event.event_time
            and self.event_type == str(event.event_type)
            and self.producer == event.producer
            and self.operation == _facet_value(facets.get(LANCE_RUN_FACET), "operation")
            and self.author == _facet_value(facets.get(AUTHOR_RUN_FACET), "name")
            and [(d.namespace, d.name) for d in self.inputs] == [(d.namespace, d.name) for d in event.inputs]
            and (self.output.namespace, self.output.name) == (event.outputs[0].namespace, event.outputs[0].name)
        )

    def to_json(self) -> str:
        """The document as compact JSON — what goes into the ``pa.json_()`` cell."""
        return self.model_dump_json(by_alias=True, exclude_none=True)


def _facet_value(facet: Any, key: str) -> str | None:  # noqa: ANN401 — custom facet bags are arbitrary JSON
    """One string field of a custom run facet, or ``None`` when the facet/key is absent or not a string."""
    if not isinstance(facet, dict):
        return None
    value = facet.get(key)
    return value if isinstance(value, str) else None


def parse_doc(raw: str | bytes) -> LineageDoc:
    """Parse a ``lineage`` cell back into a document (raises :class:`pydantic.ValidationError`)."""
    return LineageDoc.model_validate_json(raw)


def as_json_rows(doc: LineageDoc, rows: int) -> list[str]:
    """``rows`` copies of the document's JSON — the constant column a stage write stamps.

    Per-row rather than dataset metadata because the consume layer's unit is the ROW: a projection,
    join, or export that carries rows out of the dataset carries their provenance with them.
    """
    return [doc.to_json()] * rows


__all__ = [
    "AUTHOR_RUN_FACET",
    "LANCE_RUN_FACET",
    "LINEAGE_DOC_SCHEMA",
    "DatasetRef",
    "LineageDoc",
    "LineageEdge",
    "as_json_rows",
    "parse_doc",
]
