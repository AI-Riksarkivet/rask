"""A service that declares authorization must also CONNECT it.

Third occurrence of one bug, which is what makes it a gate rather than three fixes.

`make_auth_deps.get_checker` and the services' own `authorize`/`require_*` deps are fail-CLOSED by
design: FGA enabled with no client on `app.state` raises `ServiceUnavailableError`. That is the right
posture — a broken authz layer must never degrade into an open one — and it has a consequence nobody
notices from inside a service: a door that is declared but never wired answers **503 on every
request**, and every signal an operator has says the feature shipped. The settings bind, the chart
renders the env, the routes declare the dependency, and the service's own governance tests pass
because they override the very dependencies that would have 503'd.

The record:

* `compute` and `controlplane` shipped `GovernedAuthSettings` + gated routes with no `attach_auth`.
  Measured on the live estate, fixed by `f24fae39` (2026-08-26). `compute/lifespan.py` still carries
  the warning it left behind.
* `search` shipped the same shape six days later in `3593381c` — my own X6 fix — with that warning
  sitting unread one directory over.

So the invariant is checked here, once, over every service rather than remembered per service: if a
service's settings mix in `GovernedAuthSettings`, some module in that service must build its client
through the estate's shared bootstrap — `attach_auth` or `build_fga_client`. It once also accepted an
inline `state.fga` assignment, because ten services still hand-rolled the block and this gate was only
asking whether the wire was CONNECTED. `DUP-01` collapsed those ten onto the helper, so the looser
alternative now admits only a re-introduced copy; `tests/unit/test_governed_bootstrap_is_one_
implementation.py` is where that is refused, and this gate no longer contradicts it.

WHAT THIS GATE DOES NOT CATCH, stated so nobody reads more into a green run: it is a source-level
check that a service wires FGA *somewhere*, not that a particular route is reachable. Only running
the real lifespan proves that, which is what each service's own test does
(`services/search/tests/test_the_search_door_is_wired.py`).
"""

from __future__ import annotations

import ast
import pathlib


REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICES = REPO / "services"

#: Any of these, anywhere in the service's source, means the wire is connected. Both are the shared
#: bootstrap: `attach_auth` assigns onto `app.state`, `build_fga_client` returns the client for the one
#: consumer (`maintenance`) that lives outside a lifespan.
_WIRES = ("attach_auth", "build_fga_client")


def _service_sources(service: pathlib.Path) -> list[pathlib.Path]:
    return list((service / "src").rglob("*.py")) if (service / "src").is_dir() else []


def _declares_governed_auth(sources: list[pathlib.Path]) -> pathlib.Path | None:
    """The file where this service mixes `GovernedAuthSettings` into its own settings class."""
    for path in sources:
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                if name == "GovernedAuthSettings":
                    return path
    return None


def test_the_gate_can_see_the_services_at_all() -> None:
    """A guard on the guard: a layout change that emptied this walk would make it pass vacuously."""
    governed = [s.name for s in sorted(SERVICES.iterdir()) if s.is_dir() and _declares_governed_auth(_service_sources(s))]
    assert len(governed) >= 8, f"only {governed} appear to declare GovernedAuthSettings — the walk is not seeing the estate"


def test_every_service_that_declares_authorization_also_wires_it() -> None:
    unwired: list[str] = []
    for service in sorted(SERVICES.iterdir()):
        if not service.is_dir():
            continue
        sources = _service_sources(service)
        declared_at = _declares_governed_auth(sources)
        if declared_at is None:
            continue
        if any(any(wire in path.read_text() for wire in _WIRES) for path in sources):
            continue
        unwired.append(f"{service.name} (declares GovernedAuthSettings at {declared_at.relative_to(REPO)})")

    assert not unwired, (
        "these services declare authorization and never put a client on `app.state.fga`, so every "
        "gated route answers 503 'Authorization is enabled but unavailable' — the door is shut, not "
        "guarded, and nothing else in the estate reports it:\n  " + "\n  ".join(unwired)
    )
