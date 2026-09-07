"""No service in the estate depends on a compute engine. That is what BYO means.

G1b. The two seams — `service_kit.lakehouse.executor` for compute and `.saga` for the workflow
engine — name no engine, and a sibling gate pins that. This one asks the question that actually
decides whether the decoupling is real: does the DEPLOYED path go through the port, or around it?

A PORT WITH A DEAD ADAPTER IS A DECOUPLING CLAIM, NOT A DECOUPLED SYSTEM. Measured 2026-09-07,
before this gate existed: `RayJobExecutor` was constructed nowhere outside tests, the only adapter
anyone built was `InProcessExecutor`, and the live Ray lane went through `ray_submit.py` — a second,
older submission seam the port was written to replace. So the decoupling was real for the in-process
lane and fictional for the lane the estate runs.

WHY THE DEPENDENCY GRAPH IS THE TEST AND NOT THE IMPORT LIST. An import can be moved behind a
function and the coupling survives; a DECLARED dependency is what forces a compute engine into the
service's resolution, its lock and its image. `rayjob_executor.py` proves the target is reachable —
it submits a RayJob CR over HTTPX and imports no Ray at all. So when the call sites move, the
declaration can go, and BYO stops being a property of a Protocol and becomes a property of the
dependency graph: swapping the engine cannot touch a service, because no service names one.
"""

from __future__ import annotations

import pathlib
import tomllib


REPO = pathlib.Path(__file__).resolve().parents[2]

#: Distribution names that ARE a compute engine or bind one. `ray-kit` is the estate's Ray wrapper,
#: so declaring it couples the declarer to Ray exactly as `ray` itself would.
COMPUTE_ENGINES = frozenset({"ray", "ray-kit", "ray[default]", "flyte", "flytekit", "dask", "prefect"})

#: THE ONE EXEMPTION, AND IT IS A ROLE RATHER THAN AN EXCUSE. Engine knowledge is allowed to exist —
#: it has to, something must actually talk to the engine — but only inside an ADAPTER, never inside a
#: consumer. `services/compute` IS the adapter: its whole surface is `/api/ray/*` dashboard
#: introspection and the `/api/serve/*` proxy, a thin shell over `ray-kit`. Swapping the engine
#: replaces or drops that service, which is exactly what an adapter is for. `medallion` is a
#: CONSUMER — it runs stages and training, operations no engine's name belongs in — so it must reach
#: compute through `service_kit.lakehouse.executor` like anything else.
ENGINE_ADAPTERS = frozenset({"compute"})


def _declared(pyproject: pathlib.Path) -> set[str]:
    """Distribution names a package declares, with extras and version specifiers stripped."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    raw: list[str] = list(data.get("project", {}).get("dependencies") or [])
    for extra in (data.get("project", {}).get("optional-dependencies") or {}).values():
        raw.extend(extra)
    names: set[str] = set()
    for spec in raw:
        name = spec.split(";")[0].strip()
        for stop in ("[", ">", "<", "=", "!", "~", " "):
            name = name.split(stop)[0]
        if name:
            names.add(name.strip().lower())
    return names


def _services() -> list[pathlib.Path]:
    return sorted((REPO / "services").glob("*/pyproject.toml"))


def test_the_gate_can_see_the_services() -> None:
    """A glob matching nothing would make the assertion below vacuous."""
    found = _services()
    assert len(found) >= 10, f"only {len(found)} service pyprojects found — has the layout moved?"


def test_no_service_DECLARES_a_compute_engine() -> None:
    """THE GATE. A declared dependency is what puts an engine in the service's resolution, its lock
    and its image — so this is the line between a decoupling claim and a decoupled system."""
    offenders: dict[str, set[str]] = {}
    for pyproject in _services():
        if pyproject.parent.name in ENGINE_ADAPTERS:
            continue
        engines = _declared(pyproject) & COMPUTE_ENGINES
        if engines:
            offenders[pyproject.parent.name] = engines
    assert not offenders, (
        f"these services declare a compute engine: { {k: sorted(v) for k, v in offenders.items()} } — "
        "the platform's compute seam is `service_kit.lakehouse.executor`, and a service that names an "
        "engine cannot have it swapped without touching the service"
    )


def test_the_lakehouse_services_import_no_engine() -> None:
    """The narrower property that already holds, kept so a regression is caught where it happens
    rather than at the next audit. Measured 2026-09-07: 0 in all four."""
    for service in ("catalog", "lineage", "maintenance", "notifications"):
        src = REPO / "services" / service / "src"
        if not src.exists():
            continue
        offenders = [
            str(path.relative_to(REPO))
            for path in src.rglob("*.py")
            if any(
                line.startswith(("import ray", "from ray ", "from ray.", "from ray_kit", "import ray_kit"))
                for line in path.read_text(encoding="utf-8").splitlines()
            )
        ]
        assert not offenders, f"services/{service} imports a compute engine: {offenders}"


def test_the_adapter_exemption_is_NOT_a_blanket() -> None:
    """An exemption list nobody checks becomes the place engines hide. `compute` must still BE the
    adapter it is exempted as — a service that stopped talking to Ray and kept the dependency is
    carrying one for nothing, and one that grew a second engine is no longer a single adapter."""
    for name in ENGINE_ADAPTERS:
        declared = _declared(REPO / "services" / name / "pyproject.toml") & COMPUTE_ENGINES
        assert declared, f"services/{name} is exempted as an engine adapter but declares no engine — drop the exemption"
        assert len(declared) == 1, f"services/{name} is exempted as THE adapter for one engine but declares {sorted(declared)}"
