"""A project's DECLARED quality gate must apply whether or not lineage emission is enabled.

THE DEFECT. `resolve_effective_gate` recovers the project by calling `lineage.project_for(...)` — the
LINEAGE EMITTER's method. With `LANCE_LINEAGE_EMIT_ENABLED=false` the emitter is a `NoopEmitter` whose
`project_for` returns `None` unconditionally, so no project is found, no declaration is loaded, and
every project's declared gate is silently not applied. Publish still answers 200.

Measured by flipping exactly that one variable on one catalog: the declared record was still returned
by `gate/describe`, and the publish it governs ran under the request's own weaker terms.

Why this is worse than a missing feature: the declaration exists precisely so an external writer — the
party trusted least — cannot hand itself a weaker gate than a mover gets. Routing that policy through
an OBSERVABILITY switch means turning off telemetry silently turns off a governance control, and the
two have no reason to be connected.

The fix is not to make the emitter smarter. `warehouses.project_for_namespace` is the authoritative
answer — two registry reads, the binding names the warehouse and the warehouse names the project — and
it is what the emitter itself calls. The gate reads it directly, so quality policy no longer borrows
another subsystem's plumbing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from catalog.api.v1.endpoints.publication import resolve_effective_gate
from service_kit.lakehouse import gate_specs
from service_kit.lakehouse.gate_specs import GateSpec


PROJECT = "acme"
TOP_NS = "acme-gold"


class _NoopLineage:
    """Exactly what the catalog builds when `LANCE_LINEAGE_EMIT_ENABLED` is false."""

    async def project_for(self, _top_ns: str) -> str | None:
        return None


class _Body:
    key_column = "id"
    required_columns: tuple[str, ...] = ()


@pytest.fixture
def registry_root(tmp_path: Path) -> str:
    root = tmp_path / "control"
    root.mkdir()
    gate_specs.put_spec(
        str(root),
        {},
        GateSpec(project=PROJECT, key_column="declared_id", required_columns=["must_have"]),
    )
    return str(root)


@pytest.mark.asyncio
async def test_the_declared_gate_applies_with_lineage_emission_OFF(registry_root: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline: turning off telemetry must not turn off a governance control."""
    # The binding the authoritative resolver reads — stubbed at the module the gate should be calling,
    # so a fix that merely made the EMITTER smarter would not pass this.
    from catalog.api.v1.endpoints import publication as door

    monkeypatch.setattr(door.warehouses, "project_for_namespace", lambda *_a, **_k: PROJECT, raising=False)

    settings = SimpleNamespace(registry_root=registry_root, storage_options=lambda: {})
    effective = await resolve_effective_gate(settings, {}, _NoopLineage(), [TOP_NS], _Body())

    assert effective.key_column == "declared_id", (
        "the declared gate was not applied with lineage emission off — a project's quality policy "
        "must not depend on an observability switch"
    )
    assert "must_have" in effective.required_columns
