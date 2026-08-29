"""The SourceAdapter registry — invariant I1: sources are registered, never hardcoded.

The defect this replaces: IIIF was welded across TWELVE medallion files (open_ingest.md §0 C2) — a
route, a settings block, a produce module, a Ray entrypoint, event schemas. Adding S3-prefix meant
repeating all of it, which is why `S3PrefixSource` sat written-and-unwired for months.

Here a source is ONE registry entry: a factory that builds a `SourceAdapter` from a spec, plus the
lineage-input twin that names it in the graph. No new endpoint, no settings block, no topic. The
gate is A9 — adding a source may touch only an adapter, a registry entry, a lineage twin, and its
own test; a diff that touches more has re-welded something.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, Field


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from service_kit.lakehouse.sources import SourceAdapter, SourceObject


class SourceSpec(BaseModel):
    """What a caller asks to ingest. Deliberately open: `kind` selects, `options` is adapter-owned.

    I2 lives here too — there is no dataset PATH in the spec. The caller names `{project, dataset}`
    and the catalog resolves where that is. A fixed per-lane URI is why volume B overwrote volume A.
    """

    kind: str = Field(description="registry key, e.g. 's3-prefix' | 'local-dir'")
    project: str
    dataset: str
    options: dict[str, object] = Field(default_factory=dict)


class LineageInput(BaseModel):
    """The external source as it appears in the lineage graph — `iiif://host`, `s3://bucket`."""

    namespace: str
    name: str


class SourceOption(BaseModel):
    """One field a kind needs in `options`, described well enough for a UI to render it.

    The adapter owns `options`, so the adapter is the only place that can say what belongs in it.
    Without this the registry knows the kinds but not what any of them wants, and a form has to
    restate every adapter's fields in TypeScript — which re-welds the sources into the frontend after
    I1 pulled them out of the backend. Adding a kind would then mean editing a zone, and the whole
    point of the registry is that it does not.

    Deliberately presentational-minimum: a name, a label, whether it is required, and a hint. Not a
    validation schema — the adapter already validates (`local-dir source requires options.root`), and
    a second copy of those rules in a different language is a copy that goes stale.
    """

    name: str
    label: str
    required: bool = False
    numeric: bool = False
    placeholder: str | None = None
    help: str | None = None


class SourceDescriptor(BaseModel):
    """A registered kind as the outside world sees it: its key, what it needs, and whether it WORKS."""

    kind: str
    label: str
    description: str | None = None
    options: list[SourceOption] = Field(default_factory=list)
    #: Whether this deployment can actually run this kind right now.
    #:
    #: ADVERTISED-BUT-UNUSABLE IS THE STATE THIS FIXES. `lance-append` is registered unconditionally
    #: while `RASK_INGEST_LANCE_ROOT` defaults to empty, so a form built from this list offered a
    #: kind that refuses every run — and the refusal named a missing env var to a person who cannot
    #: set one.
    #:
    #: Advertised rather than HIDDEN, because the estate's ruling is "show disabled, never hide": a
    #: kind that vanishes is indistinguishable from one that was never built, and an operator
    #: reading the form has no way to learn that a knob would enable it. The reason travels with it.
    available: bool = True
    #: Why not, when `available` is False — rendered beside the disabled option. Names the knob.
    unavailable_reason: str | None = None


#: The three registry callables are `@runtime_checkable` because `_Registration` STORES them: a
#: pydantic field typed by a bare Protocol validates with `isinstance`, which a plain Protocol
#: refuses at runtime. Storing them as `object` instead is what forced a cast at all three read
#: sites — and a cast there erases the one signature the registry exists to hold.
@runtime_checkable
class SourceFactory(Protocol):
    def __call__(self, spec: SourceSpec) -> SourceAdapter: ...


@runtime_checkable
class LineageTwin(Protocol):
    def __call__(self, spec: SourceSpec) -> LineageInput: ...


@runtime_checkable
class PartitionOf(Protocol):
    """How this kind groups its units — the value of the bronze `partition_key` column.

    Registered BESIDE the adapter for the same reason `lineage_input` is: only the adapter knows what
    a unit key means. For IIIF that is the volume (constant for a run); for an S3 prefix it is the
    containing folder. The worker must not learn this — it resolves units by URI SCHEME and knows
    nothing about volumes or prefixes, so parsing the key there would re-weld the source into the
    worker, which is exactly what I1 removed.

    Returning None is the honest answer for a kind with no meaningful grouping, and writes a null.
    """

    def __call__(self, spec: SourceSpec, key: str) -> str | None: ...


@runtime_checkable
class UnusableReason(Protocol):
    """Why THIS deployment cannot run this kind, or ``None`` when it can.

    Declared beside the factory for the same reason `partition_of` and `external_base_of` are: the
    precondition is the adapter's own business, and a registry that had to know each kind's
    environment would grow a branch per source.
    """

    def __call__(self) -> str | None: ...


@runtime_checkable
class ExternalBaseOf(Protocol):
    """The ROOT every one of this kind's unit keys lives under — or None to own the bytes.

    Registered beside the adapter for the same reason `partition_of` and `lineage_input` are: only
    the adapter knows what a unit key means, and therefore what contains it. The worker resolves
    units by URI SCHEME and must not learn about buckets or directories.

    This is the placement decision of `docs/architecture/medallion-data-flow.md`, made per corpus rather than
    defaulted. A base means the bronze `payload` column stores an EXTERNAL descriptor — the URI, not
    the bytes — so the corpus is stored once instead of once per tier (measured: 0.1% of corpus on
    disk against 100.1% for the managed form, `scripts/measure_external_blob_carry_forward.py`).

    **Returning None is not a fallback, it is a real answer, and one kind genuinely needs it.**
    `lance-append` SYNTHESISES its payload — its fetcher builds Arrow IPC from dataset fragments, so
    those bytes exist at no URI and there is nothing for a descriptor to point at. A source whose
    lifecycle is not the estate's is the other case §4.1 names. Both must own their bytes.

    The base is registered in the dataset's MANIFEST at create time, and Lance refuses an external
    URI outside a registered base (`allow_external_blob_outside_bases` defaults False, and this
    estate must never set it True — outside a base, lifecycle "remains their responsibility" and the
    pointer can dangle with nothing watching).
    """

    def __call__(self, spec: SourceSpec) -> str | None: ...


@runtime_checkable
class EndpointOf(Protocol):
    """The object-store ENDPOINT this run's bytes are on, or None for the deployment's own store.

    Registered beside the adapter for the same reason `partition_of` and `external_base_of` are:
    only the adapter knows what its `options` mean, and `publish_chunk_units` reading
    `options["endpoint"]` itself would put one kind's option name into the plane's publish path —
    the coupling I1 removed.

    It exists because the answer has to CROSS THE QUEUE. `s3-prefix` advertised an `endpoint` option
    that only its enumeration half ever read: the worker built its client from
    `RASK_S3_ENDPOINT_URL`, so a run pointed at an external store was fetched from the estate's own
    — every unit onto the DLQ at best, and at worst a same-named local bucket answering under an
    external `source_uri` with no error at all. The declared endpoint now travels on `UnitTask` and
    the fetcher resolves it (`ingest.objectstore`).

    Returning None is the ordinary answer: `local-dir` and `lance-append` address no object store,
    and an `s3-prefix` run that declares no override stays on the deployment default.
    """

    def __call__(self, spec: SourceSpec) -> str | None: ...


@runtime_checkable
class FetcherFactory(Protocol):
    """How this kind turns one unit KEY into bytes, when a URI scheme cannot say it.

    `ingest.fetch` resolves a key by SCHEME and must never learn about sources — that separation is
    the reason the module exists, and its docstring already names this hook as the escape hatch:
    "where per-source behaviour genuinely differs … it belongs to that source's ADAPTER, which can
    supply its own `Fetcher`". Until this existed the hook was documented and unwired, so a kind
    whose keys are not scheme-resolvable URIs enumerated its units and then failed EVERY fetch —
    measured on `lance-append`, whose `<uri>#fragment=<id>` keys fell through to the local-dir
    branch and were refused as being outside RASK_INGEST_LOCAL_ROOT (2026-08-23).

    Returning None — not registering one — is the ordinary case: `s3://` and `https://` keys need
    nothing here, and `drain_chunk_units` falls back to the scheme-resolved `UriFetcher`.
    """

    def __call__(self) -> Fetcher: ...


@runtime_checkable
class Fetcher(Protocol):
    """Structurally identical to `ingest.worker.Fetcher`, declared here to keep the import one-way.

    `worker` imports from `sources`; re-importing it back would make the cycle that `TYPE_CHECKING`
    blocks elsewhere in this file exist at runtime.

    STRUCTURALLY IDENTICAL is a requirement, not an observation: `drain_chunk_units` passes a
    `fetcher_for(...)` result straight into `Worker`, so the two signatures diverging is a type
    error at that seam — which is exactly what caught `source_endpoint` being added to one and not
    the other.
    """

    async def fetch(self, key: str, *, source_endpoint: str | None = None) -> bytes: ...


class _Registration(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    kind: str
    build: SourceFactory
    lineage_input: LineageTwin
    descriptor: SourceDescriptor
    partition_of: PartitionOf | None = None
    fetcher: FetcherFactory | None = None
    external_base_of: ExternalBaseOf | None = None
    endpoint_of: EndpointOf | None = None
    unusable: UnusableReason | None = None


_REGISTRY: dict[str, _Registration] = {}


def register(
    kind: str,
    build: SourceFactory,
    lineage_input: LineageTwin,
    *,
    label: str | None = None,
    description: str | None = None,
    options: Sequence[SourceOption] = (),
    partition_of: PartitionOf | None = None,
    fetcher: FetcherFactory | None = None,
    external_base_of: ExternalBaseOf | None = None,
    endpoint_of: EndpointOf | None = None,
    unusable: UnusableReason | None = None,
) -> None:
    """Register a source kind. Called at import time by the adapter modules themselves.

    `options` is what a caller must put in `SourceSpec.options` for this kind. It is declared here,
    beside the adapter that reads it, because that is the only place the two can be kept in step —
    see `SourceOption`. Omitting it registers a kind with no described fields, which is honest for an
    adapter that takes none and merely unhelpful for one that does.
    """
    if kind in _REGISTRY:
        raise ValueError(f"source kind {kind!r} is already registered")
    _REGISTRY[kind] = _Registration(
        kind=kind,
        build=build,
        lineage_input=lineage_input,
        descriptor=SourceDescriptor(kind=kind, label=label or kind, description=description, options=list(options)),
        partition_of=partition_of,
        fetcher=fetcher,
        external_base_of=external_base_of,
        endpoint_of=endpoint_of,
        unusable=unusable,
    )


def build_source(spec: SourceSpec) -> SourceAdapter:
    """Resolve a spec to an adapter, or refuse with the kinds that DO exist.

    Refusing loudly matters: an unknown kind that fell through to a default would ingest the wrong
    thing under the caller's dataset name, and bronze is the replay foundation.
    """
    reg = _REGISTRY.get(spec.kind)
    if reg is None:
        known = ", ".join(sorted(_REGISTRY)) or "<none registered>"
        raise ValueError(f"unknown source kind {spec.kind!r} — registered kinds: {known}")
    return reg.build(spec)


def lineage_input_for(spec: SourceSpec) -> LineageInput:
    """The graph node for this run's INPUT — the external system, never a governed tier (R23)."""
    reg = _REGISTRY.get(spec.kind)
    if reg is None:
        raise ValueError(f"unknown source kind {spec.kind!r}")
    return reg.lineage_input(spec)


def partition_key_for(spec: SourceSpec, key: str) -> str | None:
    """The bronze `partition_key` for one unit, as the ADAPTER defines it.

    Optional by design: a kind that registers no `partition_of` writes nulls, which is the honest
    answer for a source with no meaningful grouping. Never raises for an unknown kind — this runs on
    the write path, and a partition label is not worth failing a unit that has already been fetched.
    """
    reg = _REGISTRY.get(spec.kind)
    if reg is None or reg.partition_of is None:
        return None
    return reg.partition_of(spec, key)


def external_base_for(spec: SourceSpec) -> str | None:
    """This kind's external blob base, or None when the kind must own its bytes.

    Unlike `partition_key_for`, an unknown kind here is NOT harmless-by-default in the same way, but
    the answer is the same and for a stronger reason: None means MANAGED, which copies the bytes.
    That is the conservative direction — it costs storage, where the opposite mistake (claiming a
    base a kind's keys do not live under) is refused at write by Lance, per unit, after the fetch.
    """
    reg = _REGISTRY.get(spec.kind)
    if reg is None or reg.external_base_of is None:
        return None
    return reg.external_base_of(spec)


def source_endpoint_for(spec: SourceSpec) -> str | None:
    """The object-store endpoint this run declared, as the ADAPTER reads its own options.

    Carried onto every `UnitTask` by `publish_chunk_units`, so a worker can honour it without ever
    seeing the source spec — the same shape as `partition_key_for`, and for the same reason: the
    worker resolves units by URI scheme and must not learn what a kind's options are called.

    An unknown kind answers None, not an error: an older build's chunk can name a kind this process
    no longer registers, and the honest answer for it is the deployment default.
    """
    reg = _REGISTRY.get(spec.kind)
    if reg is None or reg.endpoint_of is None:
        return None
    return reg.endpoint_of(spec)


def fetcher_for(kind: str) -> Fetcher | None:
    """This kind's own `Fetcher`, or None to use the scheme-resolved default.

    Unknown kinds answer None rather than raising: an older build's chunk can name a kind this
    process no longer registers, and the honest response is the default fetcher, not a crash in the
    drain activity.
    """
    reg = _REGISTRY.get(kind)
    if reg is None or reg.fetcher is None:
        return None
    return reg.fetcher()


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


def describe_sources() -> list[SourceDescriptor]:
    """Every registered kind and the options it takes — the registry, readable from outside.

    This is what lets a UI offer the kinds that actually exist rather than a list someone typed. The
    compute zone's ingest form hardcoded `kind: 'iiif'` while its own comment said the door was
    source-agnostic; `S3PrefixSource` had been written, tested and unreachable for months for exactly
    that class of reason. A registry nothing can read is a registry that drifts.
    """
    return [_availability(_REGISTRY[kind].descriptor) for kind in sorted(_REGISTRY)]


def _availability(descriptor: SourceDescriptor) -> SourceDescriptor:
    """`descriptor` with `available`/`unavailable_reason` resolved against THIS deployment.

    Resolved at describe time, not at registration: the registry is built at import and the
    environment is read per request, so a knob set after the process started must not leave the form
    advertising the old answer. It is a `getenv` per kind on an admin-frequency endpoint.

    The check lives with the KIND that needs it, via `unusable_reason`, so a new source declares its
    own precondition beside its factory — the same rule `partition_of` and `external_base_of` follow.
    """
    registration = _REGISTRY[descriptor.kind]
    reason = registration.unusable() if registration.unusable else None
    if reason is None:
        return descriptor
    return descriptor.model_copy(update={"available": False, "unavailable_reason": reason})


def iter_units(adapter: SourceAdapter) -> Iterator[SourceObject]:
    """The one place the plane consumes an adapter — so 'unit' has a single definition."""
    return adapter.iter_objects()


def iter_unit_keys(adapter: SourceAdapter) -> Iterator[str]:
    """Enumerate a source's unit keys WITHOUT fetching, wherever the adapter can.

    Enumeration and fetching are separated by a queue in this plane, so they are separate transfers.
    Calling `iter_objects()` here — which the first version did — reads every object's bytes to learn
    its `uri`, discards them, and leaves the workers to fetch the same bytes again: a full second
    transfer of the entire source. On a rate-limited IIIF volume that doubles the request load on the
    very endpoint `max_ack_pending` exists to protect, and does it inside one activity with no
    backpressure at all.

    `getattr` rather than `isinstance`: `KeyedSourceAdapter` is a plain Protocol, NOT
    `runtime_checkable`, so an isinstance against it raises TypeError rather than answering — the
    same trap that let a wrongly-shaped adapter reach a live run (see `adapters.py`). Duck typing is
    the honest test, and the fallback keeps the capability optional for a source that truly cannot
    list without reading.
    """
    keyed = getattr(adapter, "iter_keys", None)
    if callable(keyed):
        return iter(keyed())
    return (obj.uri for obj in adapter.iter_objects())


def iter_versioned_unit_keys(adapter: SourceAdapter) -> Iterator[tuple[str, str | None]]:
    """Enumerate ``(key, version_token)`` pairs — the identity material the anti-join needs.

    Same duck-typed optionality as :func:`iter_unit_keys`, for the same Protocol reason. A source
    without ``iter_versioned_keys`` degrades to ``(key, None)`` — snapshot semantics: the id stays
    ``sha256(key)`` and replace-in-place is invisible. Only the S3 kind promises tokens (the
    listing ETag, free in ``list_objects_v2``); the degradation is the documented contract for
    everything else, never an error.
    """
    versioned = getattr(adapter, "iter_versioned_keys", None)
    if callable(versioned):
        return iter(versioned())
    return ((key, None) for key in iter_unit_keys(adapter))
