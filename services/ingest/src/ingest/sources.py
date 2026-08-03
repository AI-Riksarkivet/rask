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
    from collections.abc import Callable, Iterator

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


class SourceFactory(Protocol):
    def __call__(self, spec: SourceSpec) -> SourceAdapter: ...


class LineageTwin(Protocol):
    def __call__(self, spec: SourceSpec) -> LineageInput: ...


class _Registration(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    kind: str
    build: object
    lineage_input: object


_REGISTRY: dict[str, _Registration] = {}


def register(kind: str, build: SourceFactory, lineage_input: LineageTwin) -> None:
    """Register a source kind. Called at import time by the adapter modules themselves."""
    if kind in _REGISTRY:
        raise ValueError(f"source kind {kind!r} is already registered")
    _REGISTRY[kind] = _Registration(kind=kind, build=build, lineage_input=lineage_input)


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


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


def iter_units(adapter: SourceAdapter) -> Iterator[SourceObject]:
    """The one place the plane consumes an adapter — so 'unit' has a single definition."""
    return adapter.iter_objects()
