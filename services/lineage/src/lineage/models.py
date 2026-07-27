"""OpenLineage RunEvent — the subset we ingest.

OpenLineage is an external standard whose wire format is camelCase (``eventType``,
``runId``); we read it with field aliases and expose snake_case in Python.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


_MODEL = ConfigDict(extra="allow", populate_by_name=True)


class Dataset(BaseModel):
    """An OpenLineage dataset (a Lance table / source); ``name`` is the catalog id.

    ``facets`` carries the standard OpenLineage dataset facets; we read several:
    ``version`` (which Lance version a run produced), ``dataSource`` (where the table
    physically lives — the S3-compatible location), and ``tags`` (governance labels).

    The spec splits dataset metadata across THREE slots, by the facet's base type: plain
    ``DatasetFacet``s ride ``facets``, ``InputDatasetFacet``s ride ``inputFacets`` on an input, and
    ``OutputDatasetFacet``s ride ``outputFacets`` on an output. Two facets we consume are not plain
    ``DatasetFacet``s — ``OutputStatisticsOutputDatasetFacet`` is an output facet and
    ``DataQualityAssertionsDatasetFacet`` is an input facet — so the official ``openlineage-python``
    client (and therefore Spark/Airflow/dbt/Marquez producers, and our own ``lineage.seed``) serialises
    them into the typed slots, NOT into ``facets``. Reading only ``facets`` silently dropped them from
    every such producer; :meth:`facet` looks in all three.
    """

    model_config = _MODEL
    namespace: str
    name: str
    facets: dict[str, Any] = Field(default_factory=dict)

    def facet(self, name: str) -> dict[str, Any] | None:
        """The named facet from whichever spec slot the producer used, else ``None``.

        Facet names are unique across the spec's three dataset slots, so a single merged lookup is
        unambiguous and tolerates both placements: our own emitters put everything in ``facets``, the
        official client splits by base type. ``facets`` wins a (spec-impossible) collision because it is
        the slot our own producers write. ``inputFacets`` / ``outputFacets`` are read off ``model_extra``
        rather than declared as fields so the feed blob (``model_dump(by_alias=True)``) keeps a producer's
        slots EXACTLY as sent — declaring them would stamp an empty ``outputFacets`` onto every input
        dataset, a key the spec's ``InputDataset`` does not have.
        """
        extra = self.model_extra or {}
        for slot in (self.facets, extra.get("outputFacets"), extra.get("inputFacets")):
            value = slot.get(name) if isinstance(slot, dict) else None
            if isinstance(value, dict):
                return value
        return None

    @property
    def source_uri(self) -> str | None:
        """Where this dataset physically lives, from the standard ``dataSource`` facet.

        For a Lance table this is the S3-compatible URI (e.g. ``s3://lakehouse/silver/features``).
        """
        facet = self.facet("dataSource")
        uri = facet.get("uri") if facet else None
        return uri if isinstance(uri, str) and uri else None

    @property
    def fields(self) -> list[dict[str, str]]:
        """Column schema from the standard ``schema`` facet — ``[{name, type[, description]}]``.

        This is the per-version schema the lineage service persists onto the ``WROTE`` edge (#24
        prerequisite); previously the ``SchemaDatasetFacet`` was received and discarded.
        """
        facet = self.facet("schema")
        items = facet.get("fields") if facet else None
        if not isinstance(items, list):
            return []
        out: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            entry = {"name": str(item["name"]), "type": str(item.get("type") or "")}
            if item.get("description"):
                entry["description"] = str(item["description"])
            out.append(entry)
        return out

    @property
    def column_edges(self) -> list[dict[str, Any]]:
        """Flattened column-lineage edges from the standard ``columnLineage`` facet (#24).

        Each entry is one *input→output* column dependency on THIS (output) dataset:
        ``{out_field, namespace, name, field, type, subtype, masking}`` where ``(name, field)`` identify
        the SOURCE column and ``out_field`` the produced column. The first transformation (modern
        ``inputFields[].transformations``) carries the kind (``DIRECT``/``IDENTITY`` vs a real change)
        and the ``masking`` governance flag. Previously this facet was received and discarded on ingest.
        """
        facet = self.facet("columnLineage")
        fields = facet.get("fields") if facet else None
        if not isinstance(fields, dict):
            return []
        edges: list[dict[str, Any]] = []
        for out_field, spec in fields.items():
            # Guard the output key too (symmetric with the input name/field + schema-name guards): a
            # malformed facet with an empty key must not materialise a junk ``(:Column {field:""})``.
            if not isinstance(out_field, str) or not out_field:
                continue
            inputs = spec.get("inputFields") if isinstance(spec, dict) else None
            if not isinstance(inputs, list):
                continue
            # Deprecated Fields-level transformation (older / Marquez-style producers that don't emit the
            # modern per-InputField ``transformations[]``); used as a fallback so their info isn't lost.
            dep_type = str(spec.get("transformationType") or "") if isinstance(spec, dict) else ""
            dep_desc = str(spec.get("transformationDescription") or "") if isinstance(spec, dict) else ""
            for inp in inputs:
                if not isinstance(inp, dict) or not inp.get("name") or not inp.get("field"):
                    continue
                transforms = inp.get("transformations")
                transforms = [t for t in transforms if isinstance(t, dict)] if isinstance(transforms, list) else []
                first = transforms[0] if transforms else {}
                # masking is a governance bit — true if ANY transformation masks (never lost to a non-first
                # one), with a fallback for the deprecated facet's MASKED transformation type.
                masking = any(t.get("masking") for t in transforms) or dep_type.upper().startswith("MASK")
                edges.append(
                    {
                        "out_field": str(out_field),
                        "namespace": str(inp.get("namespace") or ""),
                        "name": str(inp["name"]),
                        "field": str(inp["field"]),
                        "type": str(first.get("type") or "") or dep_type,
                        "subtype": str(first.get("subtype") or ""),
                        "description": str(first.get("description") or "") or dep_desc,
                        "masking": bool(masking),
                    }
                )
        return edges

    @property
    def statistics(self) -> tuple[int | None, int | None] | None:
        """Observed ``(row_count, size_bytes)`` from the standard ``outputStatistics`` facet, else ``None``.

        Present only on a real measured write (the medallion fake-Ray / lance-ray compute reads the exact
        rows + on-disk bytes it produced); a dummy emit or an input dataset omits the facet. These are the
        runtime-measured numbers that move our lineage from producer-declared toward Marquez-grade. Both
        fields are optional in the spec, so a partial facet (only one present) reports ``None`` for the
        absent half — never a fabricated ``-1`` that ``producers()`` would serve as a real measurement.
        Returns ``None`` (the whole tuple) only when NEITHER field is present.
        """
        facet = self.facet("outputStatistics")
        if facet is None:
            return None
        row_count, size = facet.get("rowCount"), facet.get("size")
        if not isinstance(row_count, int) and not isinstance(size, int):
            return None
        return (
            row_count if isinstance(row_count, int) else None,
            size if isinstance(size, int) else None,
        )

    @property
    def quality_assertions(self) -> list[dict[str, Any]]:
        """Validator checks from the standard ``dataQualityAssertions`` facet — ``[{assertion, success,
        column?}]``.

        Emitted by a medallion stage that validated the dataset it produced (the quality gate). Empty when
        the run carried no assertions (gate off / no compute). A ``success: false`` entry is the durable
        record of a batch the gate BLOCKED from promotion — the WROTE edge keeps both the failed assertion
        and the version it wrote, so the bad batch is auditable.
        """
        facet = self.facet("dataQualityAssertions")
        items = facet.get("assertions") if facet else None
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("assertion"):
                continue
            entry: dict[str, Any] = {
                "assertion": str(item["assertion"]),
                "success": bool(item.get("success")),
            }
            if item.get("column"):
                entry["column"] = str(item["column"])
            out.append(entry)
        return out

    @property
    def tags(self) -> list[str]:
        """Governance labels from the standard ``tags`` facet, as ``key=value`` strings."""
        facet = self.facet("tags")
        items = facet.get("tags") if facet else None
        if not isinstance(items, list):
            return []
        labels: list[str] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            value = item.get("value")
            labels.append(f"{item['key']}={value}" if value not in (None, "") else str(item["key"]))
        return labels


class Job(BaseModel):
    """The compute job that produced a run — for us a Ray job (Ray is the compute engine).

    ``facets`` carries the standard ``ownership`` (who owns the job) and ``jobType``
    (``processingType`` BATCH/STREAMING, ``integration`` = RAY, ``jobType`` = ETL /
    TRANSFORMATION) facets.
    """

    model_config = _MODEL
    namespace: str
    name: str
    facets: dict[str, Any] = Field(default_factory=dict)

    @property
    def source_location(self) -> dict[str, str] | None:
        """Where the job's code lives, from the standard ``sourceCodeLocation`` facet — ``{type, url,
        path?, branch?, …}``.

        A here-dummy from the medallion emitter today (the ``services/medallion`` git location); rask's
        runner will auto-derive it (its git repo + pipeline path) when lance-ray OpenLineage lands. ``None``
        when the event carries no such facet. ``type`` + ``url`` are required; other string keys pass through.
        """
        facet = (self.facets or {}).get("sourceCodeLocation")
        if not isinstance(facet, dict):
            return None
        typ, url = facet.get("type"), facet.get("url")
        if not (isinstance(typ, str) and typ and isinstance(url, str) and url):
            return None
        out = {"type": typ, "url": url}
        for key in ("repoUrl", "path", "branch", "tag", "version"):
            value = facet.get(key)
            if isinstance(value, str) and value:
                out[key] = value
        return out


class Run(BaseModel):
    """A single run of a job. ``facets.author`` carries the OIDC sub when present."""

    model_config = _MODEL
    run_id: str = Field(alias="runId")
    facets: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    """An OpenLineage run event (START/RUNNING/COMPLETE/FAIL/ABORT) with its inputs and outputs."""

    model_config = _MODEL
    event_type: str = Field(alias="eventType")
    event_time: str = Field(alias="eventTime")
    producer: str | None = Field(default=None)
    run: Run
    job: Job
    inputs: list[Dataset] = Field(default_factory=list)
    outputs: list[Dataset] = Field(default_factory=list)

    @property
    def author(self) -> str | None:
        """The run author (OIDC sub) — our custom ``author`` run facet, else standard ``ownership``.

        Prefers the ``author`` run facet (set by our producers/catalog); falls back to the
        first owner in the standard ``ownership`` job facet so events from external
        OpenLineage producers (Marquez-style) still attribute an owner.
        """
        author = (self.run.facets or {}).get("author")
        if isinstance(author, dict):
            name = author.get("name") or author.get("sub")
            if name:
                return name
        elif isinstance(author, str) and author:
            return author
        owners = ((self.job.facets or {}).get("ownership") or {}).get("owners")
        if isinstance(owners, list):
            for owner in owners:
                if isinstance(owner, dict) and owner.get("name"):
                    return owner["name"]
        return None

    @property
    def error_message(self) -> str | None:
        """The failure message from the standard ``errorMessage`` run facet, if any."""
        facet = (self.run.facets or {}).get("errorMessage")
        if isinstance(facet, dict):
            message = facet.get("message")
            return message if isinstance(message, str) and message else None
        return None

    @property
    def progress(self) -> tuple[int, int] | None:
        """Batch progress ``(done, total)`` from our custom ``progress`` run facet, if present.

        The facet rides RUNNING events (OpenLineage has no standard progress facet); the live
        run-status board reads it to render a % bar. Returns ``None`` when the event omits it —
        or carries non-numeric fields: a raise here would poison-message the Dapr ingest (RETRY
        redelivers the same malformed event forever), so malformed means no progress, never a 500.
        """
        facet = (self.run.facets or {}).get("progress")
        if isinstance(facet, dict):
            try:
                return int(facet["done"]), int(facet["total"])
            except (KeyError, TypeError, ValueError):
                return None
        return None

    @property
    def is_success(self) -> bool:
        """A terminal *successful* run — only these assert produced data / lineage."""
        return self.event_type.upper() == "COMPLETE"

    @property
    def operation(self) -> str | None:
        """The catalog operation (e.g. ``create_table``) from the ``lance`` run facet, if any.

        Set by the catalog's emitter (``catalog.core.lineage_emit``); used to attach the
        ``(:User)-[:CREATED]->(:Dataset)`` edge on a table-create event.
        """
        lance = (self.run.facets or {}).get("lance")
        return lance.get("operation") if isinstance(lance, dict) else None

    def output_version(self, name: str) -> str | None:
        """The Lance dataset version this run produced for output ``name`` (``version`` facet).

        Ties a provenance run to the exact dataset version it wrote, so two refinement passes
        over the same table (e.g. add a column, then another) are distinguishable.
        """
        for ds in self.outputs:
            if ds.name == name:
                facet = ds.facet("version")
                version = facet.get("datasetVersion") if facet else None
                return str(version) if version is not None else None
        return None

    def input_version(self, name: str) -> str | None:
        """The Lance dataset version this run CONSUMED for input ``name`` (``version`` facet).

        The read twin of :meth:`output_version`. A producer that pins its inputs — the Ray TRAIN job
        pins every feature dataset (#115 D1: training on floating LATEST is not reproducible) — says
        so with the same spec ``version`` facet on the input. Without persisting it the pin is emitted
        and then dropped, and the graph can no longer answer the question the pin exists to answer:
        *which exact feature versions produced this model?* (Found live 2026-07-13: 280 READ edges,
        zero versions.) Producers that read floating LATEST simply omit the facet → ``None``.
        """
        for ds in self.inputs:
            if ds.name == name:
                facet = ds.facet("version")
                version = facet.get("datasetVersion") if facet else None
                return str(version) if version is not None else None
        return None
