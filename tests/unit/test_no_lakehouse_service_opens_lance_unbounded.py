"""No lakehouse service opens a Lance dataset without a bounded session.

[[LH-096]]. Every lakehouse pod runs a 512 Mi limit (128 Mi request). A bare `lance.dataset(uri)` takes
Lance's DEFAULT ceilings — 1 GiB metadata, 6 GiB index — serves one call and discards the cache WITH the
handle, so the process carries caps describing a container it is not running in AND never caches
anything. `rask-maintenance` was OOMKilled (exit 137) on 2026-09-10 before it clamped; the live catalog
now logs `lance_cache_clamped_to_container granted_bytes=214748364 container_budget_bytes=214748364
fraction=0.4`, i.e. 384 MB configured reduced to exactly 0.4 x the pod's own cgroup limit.

ONE GATE, NOT ONE PER SERVICE. This rule is estate-wide, and a per-service copy is how three
half-enforced versions of one invariant drift apart — catalog had its own structural check until this
replaced it, and the behavioural proof (that a shared session actually POPULATES, which a `session=`
kwarg assertion cannot show) stays where it is, beside the catalog's own session.

A SESSION IS NOT A HANDLE CACHE, which is why this is safe to require everywhere. Caching a DATASET
pins a version and needs a freshness contract; a `Session`'s keys carry `(uri, version, etag)`, so a
compaction writes NEW keys and a stale read is not expressible.

MAINTENANCE IS IN SCOPE TOO — it is one of the four lakehouse services and shipped this first.
service-kit is NOT, and the omission is deliberate: its opens are in code SHARED by services whose caps
may differ, so threading a session there is a signature question rather than a conversion, and
pretending otherwise would let this gate pass while shared code still mints the defaults.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]

#: The lakehouse is these four services. `ingest` is phase 2 and `viewer` is parked, so neither is held
#: to this yet — naming them here rather than leaving them out silently.
LAKEHOUSE_SERVICES = ("catalog", "lineage", "medallion", "maintenance")


def _bare_opens(service: str) -> list[str]:
    """`file:line` for every `lance.dataset(...)` in the service that passes no `session=`."""
    root = REPO / "services" / service / "src"
    bare: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "dataset" or not isinstance(node.func.value, ast.Name) or node.func.value.id != "lance":
                continue
            if not any(kw.arg == "session" for kw in node.keywords):
                bare.append(f"{path.relative_to(root)}:{node.lineno}")
    return bare


@pytest.mark.parametrize("service", LAKEHOUSE_SERVICES)
def test_the_service_opens_lance_only_through_a_bounded_session(service: str) -> None:
    """Exhaustive, not a sample: ONE missed site is a path that still mints 1 GiB + 6 GiB per open."""
    bare = _bare_opens(service)

    assert not bare, f"{service} opens Lance without a session at: {bare}"


@pytest.mark.parametrize("service", LAKEHOUSE_SERVICES)
def test_the_service_can_build_its_shared_session(service: str) -> None:
    """The gate above is satisfiable by deleting every open, so this pins that the seam exists — each
    lakehouse service exposes one process-wide session whose caps are clamped to its container."""
    module = {
        "catalog": "catalog.core.config",
        "lineage": "lineage.core.config",
        "medallion": "medallion.core.config",
        "maintenance": "maintenance.core.config",
    }[service]
    imported = __import__(module, fromlist=["shared_lance_session"])

    assert callable(imported.shared_lance_session), f"{service} exposes no shared_lance_session"
