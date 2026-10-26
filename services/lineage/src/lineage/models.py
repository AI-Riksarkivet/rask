"""OpenLineage RunEvent — the subset we ingest.

OpenLineage is an external standard whose wire format is camelCase (``eventType``,
``runId``); we read it with field aliases and expose snake_case in Python.
"""

from __future__ import annotations

from typing import Any, Final

from lance_namespace import PermissionDeniedError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from lineage_kit.signing import SIGNATURE_FACET, RefusalReason
from service_kit.openlineage import BRANCH_IDENTIFIER, LANCE_FACET, static_event_id


_MODEL = ConfigDict(extra="allow", populate_by_name=True)

#: The most datasets one event may name as inputs, and as outputs ([[LIN-6]]). A successful run records an edge per
#: (input, output) pair, so the two bounds are what one event can cost the edge list; 64 is the catalog's own bound on
#: a run's inputs (`catalog.core.run_context.MAX_INPUTS`), so no event the catalog emits is refused for it.
MAX_DATASETS: Final = 64
#: The most one event may weigh as JSON, all of which its feed row stores ([[LIN-6]]). The bus carries at most NATS's
#: default `max_payload`, 1 MiB, which the NATS fixture does not raise, so this refuses at the HTTP door only an event
#: the bus could never have carried.
MAX_EVENT_BYTES: Final = 1024 * 1024


def _within_weight(event: BaseModel) -> None:
    """Refuse ``event`` when its JSON outweighs :data:`MAX_EVENT_BYTES`; raised inside validation, it is a malformed event."""
    weight = len(event.model_dump_json(by_alias=True).encode())
    if weight > MAX_EVENT_BYTES:
        raise ValueError(f"the event weighs {weight} bytes as JSON; lineage keeps at most {MAX_EVENT_BYTES}")


class ColumnUpstream(BaseModel):
    """A dataset a ``columnLineage`` facet names as the source of one of an output's columns.

    The ingest authorizer reads these: recording "my column came from yours" is a claim to have read yours.
    """

    namespace: str = ""
    name: str


#: The run operations that maintain a table rather than write it: they need `can_maintain` at the ingest
#: door. `expire_versions` is maintenance's version reclamation; `compact_table` and `create_index` are the
#: catalog's names for a bring-your-own job's compaction and index commits.
MAINTENANCE_OPERATIONS: Final = frozenset({"expire_versions", "compact_table", "create_index"})


def lineage_name_for(namespace: str, name: str) -> str:
    """The identity a dataset is recorded under for a (namespace, name) pair — see :attr:`Dataset.lineage_name`.

    Module-level so every place that derives a dataset's identity, a `Dataset` model or a raw facet dict,
    goes through ONE rule: a second copy of this expression is how an edge ends up under a different key
    than the table it is meant to name.
    """
    from lineage.api.fga_deps import is_external_source

    return f"{namespace}/{name}" if is_external_source(namespace, name) else name


class Dataset(BaseModel):
    """An OpenLineage dataset (a Lance table / source); ``name`` is the catalog id.

    ``facets`` carries the standard OpenLineage dataset facets; lineage reads ``version`` (which Lance
    version a run produced or read), the catalog's ``lance`` facet (the ref an input was read on) and
    ``columnLineage`` (whose upstreams the ingest authorizer checks).

    The spec splits dataset metadata across THREE slots, by the facet's base type: plain
    ``DatasetFacet``s ride ``facets``, ``InputDatasetFacet``s ride ``inputFacets`` on an input, and
    ``OutputDatasetFacet``s ride ``outputFacets`` on an output. The official ``openlineage-python`` client
    (and therefore Spark/Airflow/dbt/Marquez producers) serialises the typed facets into the typed slots,
    NOT into ``facets``; :meth:`facet` looks in all three.
    """

    model_config = _MODEL
    namespace: str
    name: str
    facets: dict[str, Any] = Field(default_factory=dict)

    @property
    def lineage_name(self) -> str:
        """The identity a dataset is recorded under — bare for a governed table, NAMESPACE-QUALIFIED for an external one.

        **This is a security boundary, not a naming preference.** `enforce_output_authz` exempts inputs
        whose namespace carries a URI scheme, because an external source has no catalog entry and so no
        tuple that could ever authorize it — refusing them was unsatisfiable and refused every honest
        producer permanently. That exemption is safe ONLY IF an unauthorized external reference cannot
        name a governed table: an input `{namespace: "s3://evil", name: "gold$catalog"}` skips the check, so
        an edge keyed on its bare name would claim that `gold$catalog` fed the attacker's table ([[LIN-3]]).

        Qualifying makes the property STRUCTURAL rather than heuristic: no name a caller can choose
        collides, because the discriminator being prefixed is the same one the exemption keys on. A governed
        table keeps its bare catalog id, which is what every read door and every stored row uses.
        """
        return lineage_name_for(self.namespace, self.name)

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
    def column_upstreams(self) -> list[ColumnUpstream]:
        """The datasets this (output) dataset's standard ``columnLineage`` facet names as column sources.

        Each ``fields[<column>].inputFields[]`` entry names one; an entry without a ``name`` or ``field`` names
        nothing and is skipped.
        """
        facet = self.facet("columnLineage")
        fields = facet.get("fields") if facet else None
        if not isinstance(fields, dict):
            return []
        upstreams: list[ColumnUpstream] = []
        for spec in fields.values():
            inputs = spec.get("inputFields") if isinstance(spec, dict) else None
            if not isinstance(inputs, list):
                continue
            upstreams.extend(
                ColumnUpstream(namespace=str(inp.get("namespace") or ""), name=str(inp["name"]))
                for inp in inputs
                if isinstance(inp, dict) and inp.get("name") and inp.get("field")
            )
        return upstreams


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


class Run(BaseModel):
    """A single run of a job. ``facets.author`` carries the OIDC sub when present."""

    model_config = _MODEL
    run_id: str = Field(alias="runId")
    facets: dict[str, Any] = Field(default_factory=dict)


class UnauthoredRunError(PermissionDeniedError):
    """A bus-delivered run carrying no author at all — a defect in the MESSAGE, not a verdict on a person.

    Its own type because the ack differs and nothing else separates the two: a named person who lacks a
    grant can be granted one, and the same event then succeeds on its next presentation, so that refusal
    keeps the DROP that routes it to the dead-letter topic. An author cannot be added to bytes already
    published — no tuple, no redelivery and no restart changes the answer — so re-presenting it buys
    nothing and parking it buys a duplicate.

    Measured on the deployed estate 2026-09-18: 37 of 44 refusals in one hour were this, all for ONE run
    id, in a single burst at pod start with `already_recorded=False`, while the whole retained stream was
    replayed.

    It stops being unrepairable when the producers sign ([[LH-064]]), at which point this arm should
    stop firing rather than begin discarding more.
    """


class ForeignRunError(PermissionDeniedError):
    """An event extending a run another subject founded — refused, and no grant can change that ([[LIN-5]]).

    Its own type for the reason :class:`UnauthoredRunError` has one: the ack differs. A named person who lacks a
    grant can be granted one, so that refusal keeps the DROP that parks it for repair; whose run this is cannot be
    granted, so re-presenting the event buys nothing, and the bus acks it and counts it unrepairable.
    """


class UngovernedOutputError(PermissionDeniedError):
    """A refusal naming outputs that carry NO authorization tuple at all — nothing to grant ON.

    Its own type for the same reason :class:`UnauthoredRunError` has one: the ack differs and the
    message cannot carry the distinction. A named person denied on a GOVERNED table is repairable —
    write the tuple and the same event succeeds on its next presentation — so that refusal keeps the
    DROP that routes it to the dead-letter topic. A table FGA has no record of is a different fact: a
    grant needs an object, and re-presenting the event cannot conjure one, so parking it appends a new
    dead-letter copy per restart about an event that can never be accepted.

    Measured on the deployed estate 2026-09-19: all 7 parks in a six-hour window named four outputs —
    `e2e-ns$t74eff1b3`, `lh144b5e42f0dfns$aa`, `lh144b5e42f0dfns$bb`, `lh098bbff657cns$frag` — and
    every one carries ZERO tuples (`POST /stores/{id}/read` on `table:<id>`, 0 of 0). The first also
    answers 404 from the catalog. So the repairable arm was holding only unrepairable events, and the
    DLQ grew by exactly that set on every roll.

    THE UNKNOWN CASE IS NOT THIS TYPE. A store that cannot be read is not evidence of absence, and the
    gate raises the plain :class:`PermissionDeniedError` there — parking a repairable event costs a
    duplicate, while acking an unreadable one deletes provenance, and only the second is unrecoverable.
    """


class UnverifiedEventError(PermissionDeniedError):
    """An event no listed signer's published key verifies: unsigned, forged, or signed by an identity the chart does not list ([[LH-064]]).

    Its own type for the reason :class:`UnauthoredRunError` has one: the ack differs and the message cannot
    carry the distinction. A named person who lacks a grant keeps the DROP that routes the event to the
    dead-letter topic, because a tuple can be written and the same event then succeeds on its next
    presentation. Bytes already published cannot gain a signature, so no grant, redelivery or restart changes
    this answer: the bus ACKS it and counts it (`Outcome.SIGNATURE_REFUSED`), and the drain records it and
    retires the staged object, which is the only copy a retry would have re-read.

    ``reason`` is the kit's closed :data:`~lineage_kit.signing.RefusalReason`, the label `lineage.signature.refused`
    is keyed on; the message names what was wrong with the signature and never its bytes.

    THE OUTAGE IS NOT THIS TYPE. A public-key list that cannot be read says nothing about the signature, so
    the gate raises :class:`lance_namespace.ServiceUnavailableError` there and the delivery is retried.
    """

    def __init__(self, message: str, reason: RefusalReason) -> None:
        super().__init__(message)
        self.reason: RefusalReason = reason


def without_signature_facet(raw: dict[str, Any]) -> dict[str, Any]:
    """``raw`` without a ``rask_signature`` facet on either facet bag: what a door that cannot verify may record.

    The facet means "a listed signer's key verified these bytes" wherever lineage stores or serves an event, and
    only the bus and the outbox can check that. A caller of the HTTP door holds a bearer token and no signing
    key, so a facet it supplies is a claim and not a signature: recorded, it would sit in the feed under the name
    of a verified one. Both bags are cleared, the run's and the dataset's, rather than the one a signature would
    ride on, because which bag a reader takes is the reader's rule. Only the path to a facet is copied; the rest
    of the event is shared and not touched.
    """
    cleared = raw
    for holder in ("run", "dataset"):
        section = cleared.get(holder)
        facets = section.get("facets") if isinstance(section, dict) else None
        if isinstance(section, dict) and isinstance(facets, dict) and SIGNATURE_FACET in facets:
            kept = {name: facet for name, facet in facets.items() if name != SIGNATURE_FACET}
            cleared = {**cleared, holder: {**section, "facets": kept}}
    return cleared


class DatasetEvent(BaseModel):
    """An OpenLineage ``DatasetEvent`` — a dataset change that no job performed.

    The spec calls it "A Dataset sent within static metadata events", which is exactly a catalog DDL
    change: there is no run to name and nothing executed. Its ``"not": { "required": ["job", "run"] }``
    refuses the two TOGETHER rather than either alone — `required` is satisfied only when every listed
    property is present, so a payload carrying just one passes the schema. This model refuses EITHER,
    deliberately stricter: one of them is enough to make a static change look like something that ran, and
    a forwarded graph backend records a run and a job for an operation nobody performed.

    THE FORBIDDEN MEMBERS ARE REFUSED RATHER THAN IGNORED. ``extra="allow"`` keeps this model
    forward-compatible with spec fields it does not read yet, and that same tolerance would accept a
    ``run`` as an unread extra — re-admitting the phantom run through the new door while every test here
    passed.

    It answers ``run_id`` / ``inputs`` / ``outputs`` so the ingest authorizer needs no second
    implementation: a static change amends no run and reads no input, and the dataset it changed is the
    output the write check gates on.
    """

    model_config = _MODEL
    event_time: str = Field(alias="eventTime")
    producer: str | None = Field(default=None)
    dataset: Dataset

    @model_validator(mode="after")
    def _bounded(self) -> DatasetEvent:
        _within_weight(self)
        return self

    @model_validator(mode="before")
    @classmethod
    def _refuse_a_run_or_a_job(cls, raw: object) -> object:
        if isinstance(raw, dict):
            named = sorted(member for member in ("run", "job") if raw.get(member) is not None)
            if named:
                raise ValueError(f"a DatasetEvent may not carry {' or '.join(named)}: the spec forbids both on a static metadata event")
        return raw

    @property
    def run_id(self) -> str | None:
        """Always ``None`` — a static metadata change amends no run."""
        return None

    @property
    def inputs(self) -> list[Dataset]:
        """Always empty: this event asserts what a dataset IS, never what was read to produce it."""
        return []

    @property
    def outputs(self) -> list[Dataset]:
        """The one dataset that changed, so the ingest write-check gates on the table the DDL touched."""
        return [self.dataset]

    @property
    def operation(self) -> str | None:
        """The catalog operation (``create_table``, ``drop_columns``, …) from the ``lance`` dataset facet."""
        lance = (self.dataset.facets or {}).get("lance")
        return lance.get("operation") if isinstance(lance, dict) else None

    @property
    def feed_event_type(self) -> str:
        """The state the durable feed records this change under. A WIRE FIELD IT DOES NOT HAVE.

        The spec defines no ``eventType`` on a ``DatasetEvent`` and the stored event carries none — this
        is the feed's own projection, and it exists because both of the feed's dedup indexes key on it
        alongside ``run_id``. COMPLETE because a static fact has no other state: there is no START, no
        RUNNING, and nothing that can still fail. A non-terminal value here would also drop every DDL
        change out of the consumers that filter on terminal states, which today includes the one that
        decides whether a person is told.
        """
        return "COMPLETE"

    @property
    def feed_id(self) -> str:
        """The id the durable feed stores this change under — derived from its content, never minted.

        A static change has no run id, so the feed keys it under :func:`service_kit.openlineage.static_event_id`.
        ONE derivation for the writer (`services.provenance.feed_key`) and the reader that asks whether this
        exact event is already there (`api.fga_deps._is_replay`), so the two cannot look in different places.
        """
        return static_event_id(
            producer=self.producer or "",
            namespace=self.dataset.namespace,
            name=self.dataset.name,
            operation=self.operation or "",
            event_time=self.event_time,
        )

    @property
    def is_success(self) -> bool:
        """Always true: a static metadata change is a fact, not an attempt that could still fail.

        Answered so the column-inventory seeding — which a table create is the only source of — runs on
        this event exactly as it does on a run's, rather than needing a second copy gated differently.
        """
        return True

    @property
    def author(self) -> str | None:
        """Who made the change — rask's ``author`` dataset facet, else the standard ``ownership`` facet.

        Same preference order as :attr:`RunEvent.author` and for the same reason: this is ATTRIBUTION,
        where a producer's claimed owner beats no owner. It is NOT what authorizes the event — see
        :func:`author_sub_from_payload`, which takes the verified ``sub`` and nothing else.
        """
        facets = self.dataset.facets or {}
        author = facets.get("author")
        if isinstance(author, dict) and (name := author.get("name") or author.get("sub")):
            return str(name)
        ownership = facets.get("ownership")
        if isinstance(ownership, dict):
            owners = ownership.get("owners")
            if isinstance(owners, list) and owners and isinstance(owners[0], dict) and (owner := owners[0].get("name")):
                return str(owner)
        return None


def parse_event(data: object) -> RunEvent | DatasetEvent:
    """One payload as whichever OpenLineage event it is — a run, or a change no job performed.

    DISCRIMINATED ON `dataset` WITHOUT A `run`. A `DatasetEvent` requires `dataset`, and while the
    schema only refuses `run` and `job` TOGETHER, this estate treats either as malformed: one of them
    is enough to make a static change look like something that ran. `DatasetEvent` refuses it
    explicitly and the caller reports it as malformed — the same answer a payload that is neither gets.

    The order matters only for the malformed case: anything carrying `run` goes to `RunEvent`, so a
    real run cannot be diverted here by a stray `dataset` key.

    IT LIVES IN `models` BECAUSE BOTH DOORS NEED IT. The bus door had the only copy while the HTTP
    door declared its body as `RunEvent` outright, so a static event 422'd there — and a producer
    whose provenance is dropped by one door and kept by the other has no way to tell which it hit.
    """
    if isinstance(data, dict) and "dataset" in data and "run" not in data:
        return DatasetEvent.model_validate(data)
    return RunEvent.model_validate(data)


def author_sub_from_payload(raw: object) -> str | None:
    """The VERIFIED author sub inside a run-event payload, or ``None`` — tolerant of a payload that
    does not parse.

    For the DISCARD paths: a dead-lettered delivery and a poison outbox object are both terminal
    provenance loss, and both throw away a payload that CONTAINS the person it belonged to. The log
    named only the event id, so an operator could see that provenance was dropped and not whose.

    Tolerant rather than a :class:`RunEvent` parse because the poison path fires precisely BECAUSE the
    event failed validation — the strict model is unavailable exactly where the answer is needed.

    ``sub`` and nothing else, unlike :attr:`RunEvent.author` one class up. That property prefers
    ``name`` and falls back to the standard ``ownership`` job facet, which is right for ATTRIBUTION on
    a board — an external producer's claimed owner beats no owner — and wrong here: both are
    producer-supplied and unverified, so a loss record built on them could name someone who is not the
    author of the run that was lost. Anonymous beats misattributed. Same rule
    ``notifications.author_subject`` applies for the same reason.

    TWO HOMES, ONE RULE. A :class:`DatasetEvent` has no run, so its stamp rides the same reserved
    ``author`` facet on the DATASET — and it is read under exactly the same restriction. Moving DDL off
    the run must not move its author onto ``ownership``, which this is required to distrust; a producer
    that could sign there would authorize itself under anyone's display name.
    """
    if not isinstance(raw, dict):
        return None
    for holder in (raw.get("run"), raw.get("dataset")):
        if not isinstance(holder, dict):
            continue
        facets = holder.get("facets")
        if not isinstance(facets, dict):
            continue
        author = facets.get("author")
        if not isinstance(author, dict):
            continue
        sub = author.get("sub")
        if isinstance(sub, str) and sub.strip():
            return sub
    return None


def run_id_from_payload(raw: object) -> str | None:
    """The run id inside a run-event payload, or ``None`` — tolerant of a payload that does not parse.

    Tolerant for the same reason :func:`author_sub_from_payload` is, and it shares that function's
    caller: the parking path fires on a delivery that already exhausted its retries, so the strict
    model is unavailable exactly where the answer is needed.

    A blank or wrong-typed id reads as "no run id", and what that buys is narrower than it looks: the
    VERDICT is the same either way — `_feed_already_holds` answers ``False`` for ``None``, and a feed
    lookup of ``""`` finds nothing and reaches the same ``False`` — so both paths record ``DEAD_LETTERED``.
    What it actually avoids is a feed query for an id that cannot match, and a `run_id` log field
    presenting a malformed value as though it were an id.
    """
    if not isinstance(raw, dict):
        return None
    run = raw.get("run")
    if not isinstance(run, dict):
        return None
    run_id = run.get("runId")
    return run_id if isinstance(run_id, str) and run_id.strip() else None


class RunEvent(BaseModel):
    """An OpenLineage run event (START/RUNNING/COMPLETE/FAIL/ABORT) with its inputs and outputs."""

    model_config = _MODEL
    event_type: str = Field(alias="eventType")
    event_time: str = Field(alias="eventTime")
    producer: str | None = Field(default=None)
    run: Run
    job: Job
    inputs: list[Dataset] = Field(default_factory=list, max_length=MAX_DATASETS)
    outputs: list[Dataset] = Field(default_factory=list, max_length=MAX_DATASETS)

    @model_validator(mode="after")
    def _bounded(self) -> RunEvent:
        _within_weight(self)
        return self

    @property
    def feed_event_type(self) -> str:
        """The state the durable feed records this event under — for a run, its own wire state."""
        return self.event_type

    @property
    def feed_id(self) -> str | None:
        """The id the durable feed stores this event under — for a run, its run id."""
        return self.run.run_id

    @property
    def run_id(self) -> str | None:
        """This event's run id, as :class:`DatasetEvent` also answers it.

        The ingest authorizer gates on the run a producer is amending, and a static metadata change
        amends none — so both event kinds answer the question and only one of them has an id. Reading
        ``event.run.run_id`` there instead would mean a second authorization path for the other kind,
        which is the drift `enforce_bus_authz` exists to prevent.
        """
        return self.run.run_id

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
    def is_success(self) -> bool:
        """A terminal *successful* run — only these assert produced data / lineage."""
        return self.event_type.upper() == "COMPLETE"

    @property
    def operation(self) -> str | None:
        """The catalog operation (e.g. ``create_table``) from the ``lance`` run facet, if any.

        Set by the catalog's emitter (``catalog.core.lineage_emit``); it decides which rung may record the
        event (``api.fga_deps.relations_for_operation``).
        """
        lance = (self.run.facets or {}).get("lance")
        return lance.get("operation") if isinstance(lance, dict) else None

    @property
    def write_ref(self) -> str | None:
        """The Lance REF this run wrote to, from the ``lance`` run facet (``ref``); ``None`` = main.

        The version number alone does not identify a snapshot: a branch is a whole parallel dataset with
        its OWN version sequence, so one number names two different states. Measured on pylance 11.0.0 —
        main at v2, a branch write gives branch v3, a later main write gives main v3, different contents.
        Stored on each edge the run asserts, so `(table, version)` names one commit.

        ``None`` for main rather than the literal ``"main"``, matching what the catalog emits: every write
        recorded before the ref existed carries none, and a literal would make them all read as some other
        ref.
        """
        ref = self._run_lance_facet().get("ref")
        return str(ref) if ref else None

    @property
    def write_ref_identifier(self) -> str | None:
        """The incarnation of :attr:`write_ref`, from the ``lance`` run facet (``branchIdentifier``); ``None`` on main.

        A branch deleted and created again under one name restarts its numbering, so ``(table, ref, N)`` names two
        commits until the incarnation is named beside it ([[LH-413]]).
        """
        if self.write_ref is None:
            return None
        identifier = self._run_lance_facet().get(BRANCH_IDENTIFIER)
        return str(identifier) if identifier else None

    def _run_lance_facet(self) -> dict[str, Any]:
        """The run's ``lance`` facet, or an empty one when it carries none."""
        lance = (self.run.facets or {}).get(LANCE_FACET)
        return lance if isinstance(lance, dict) else {}
