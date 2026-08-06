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

from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from service_kit.lakehouse.sources import SourceAdapter, SourceObject


class SourceSpec(BaseModel):
    """What a caller asks to ingest. Deliberately open: `kind` selects, `options` is adapter-owned.

    I2 lives here too — there is no dataset PATH in the spec. The caller names `{project, dataset}`
    and the catalog resolves where that is. A fixed per-lane URI is why volume B overwrote volume A.
    """

    kind: str = Field(description="registry key, e.g. 'iiif' | 's3-prefix' | 'local-dir'")
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
    """A registered kind as the outside world sees it: its key and what it needs."""

    kind: str
    label: str
    description: str | None = None
    options: list[SourceOption] = Field(default_factory=list)


class SourceFactory(Protocol):
    def __call__(self, spec: SourceSpec) -> SourceAdapter: ...


class LineageTwin(Protocol):
    def __call__(self, spec: SourceSpec) -> LineageInput: ...


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


class _Registration(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    kind: str
    build: object
    lineage_input: object
    descriptor: SourceDescriptor
    partition_of: object = None


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
    build_fn: Callable[[SourceSpec], SourceAdapter] = reg.build  # type: ignore[assignment]
    return build_fn(spec)


def lineage_input_for(spec: SourceSpec) -> LineageInput:
    """The graph node for this run's INPUT — the external system, never a governed tier (R23)."""
    reg = _REGISTRY.get(spec.kind)
    if reg is None:
        raise ValueError(f"unknown source kind {spec.kind!r}")
    twin: Callable[[SourceSpec], LineageInput] = reg.lineage_input  # type: ignore[assignment]
    return twin(spec)


def partition_key_for(spec: SourceSpec, key: str) -> str | None:
    """The bronze `partition_key` for one unit, as the ADAPTER defines it.

    Optional by design: a kind that registers no `partition_of` writes nulls, which is the honest
    answer for a source with no meaningful grouping. Never raises for an unknown kind — this runs on
    the write path, and a partition label is not worth failing a unit that has already been fetched.
    """
    reg = _REGISTRY.get(spec.kind)
    if reg is None or reg.partition_of is None:
        return None
    fn: Callable[[SourceSpec, str], str | None] = reg.partition_of  # type: ignore[assignment]
    return fn(spec, key)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


def describe_sources() -> list[SourceDescriptor]:
    """Every registered kind and the options it takes — the registry, readable from outside.

    This is what lets a UI offer the kinds that actually exist rather than a list someone typed. The
    compute zone's ingest form hardcoded `kind: 'iiif'` while its own comment said the door was
    source-agnostic; `S3PrefixSource` had been written, tested and unreachable for months for exactly
    that class of reason. A registry nothing can read is a registry that drifts.
    """
    return [_REGISTRY[kind].descriptor for kind in sorted(_REGISTRY)]


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
