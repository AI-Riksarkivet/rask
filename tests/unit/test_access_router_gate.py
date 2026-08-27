"""The estate's highest-privilege router must have ONE door, not nine.

open_fastapi-audit — "The estate's highest-privilege router — raw OpenFGA tuple write/delete — is
gated by nine hand-written calls instead of one router-level dependency".

`/v1/access` reads and WRITES the raw tuple store: the authorization state of the entire estate. Every
one of its nine handlers opened with `client = await _estate_gate(request, settings, token)`, and all
nine were verified present at HEAD — which is why the audit grades this low. It is a maintenance
hazard, not a live hole: the tenth route is the one that forgets.

`authn.md` is direct about the shape: "Apply at the router level when *every* route in the group needs
the same check — cheaper to read and harder to forget." Harder to forget is the entire point here.

TWO HALVES, because either alone leaves a gap the other covers. The router dependency fixes the SHAPE
— a new route inherits the gate instead of having to remember it. The contract test fixes the
ENUMERATION — it walks the router's actual routes, so a future route added to a DIFFERENT router, or
a dependency quietly dropped, is caught by something that counts rather than by something that reads.
"""

from __future__ import annotations

from catalog.api.v1.endpoints import access_admin as ep
from fastapi.routing import APIRoute


def _routes() -> list[APIRoute]:
    routes = [r for r in ep.router.routes if isinstance(r, APIRoute)]
    assert routes, "no routes on the access router — this gate would pass vacuously"
    return routes


def test_the_router_itself_carries_the_estate_gate() -> None:
    """One dependency on the router, so route number ten cannot be born ungated."""
    assert ep.router.dependencies, (
        "the /v1/access router declares no dependencies — its estate-admin gate is nine hand-written calls, and the tenth route is the one that forgets"
    )


def test_every_access_route_is_covered_by_that_gate() -> None:
    """The enumeration half: counted, not read.

    A router-level dependency is inherited at include time, so this asserts the property every route
    actually has rather than trusting that the declaration above reaches them.
    """
    ungated = [
        f"{sorted(route.methods or [])} {route.path}"
        for route in _routes()
        if not any(getattr(dep.call, "__name__", "") == "estate_gate" for dep in route.dependant.dependencies)
    ]
    assert not ungated, f"these access routes do not clear the estate gate: {ungated}"


def test_the_gate_is_still_the_same_check() -> None:
    """Hoisting must not quietly weaken it. `can_observe_events` on the FIXED root object is the
    platform privilege the tuple store requires — not a per-project relation, because the store is the
    whole estate's authorization state (authz scope == data scope)."""
    import inspect

    source = inspect.getsource(ep.estate_gate)
    assert "can_observe_events" in source
    assert "fga_root_object" in source
    assert "fga_enabled" in source, "the FGA-off 501 branch was dropped"
    assert "ServiceUnavailableError" in source, "the enabled-but-unwired fail-closed branch was dropped"
