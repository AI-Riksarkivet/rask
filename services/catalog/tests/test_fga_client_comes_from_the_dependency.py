"""catalog-api-09 — the FGA client is INJECTED, and the estate gate is written once.

``api/dependencies.get_fga_client``'s own docstring says it exists so there is "one place that knows
where the client lives (``app.state.fga``), injected like ``NamespaceDep`` instead of re-spelled as
``getattr(request.app.state, "fga", None)`` in every create/seed/list handler". Fourteen endpoint sites
re-spelled it anyway, so the docstring described an intention rather than the code — and a handler that
reaches into ``app.state`` by hand also carries its own idea of what an absent client means.

Two gates, both AST/text over the source:

* nothing outside ``api/dependencies.py`` (the one place that may know) names ``app.state.fga``;
* the "FGA is off / FGA is unwired" preamble that eleven handlers hand-copied has ONE body. Its
  fingerprint is the 503 message; the shared helper is ``fga_deps.require_fga``.
"""

from __future__ import annotations

import pathlib
import re


_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog"
_OWNER = _SRC / "api" / "dependencies.py"
#: The lifespan BUILDS and disposes ``app.state.fga``; it is the writer the dependency reads from.
_LIFESPAN = _SRC / "main.py"

#: The 503 half of the copied preamble — one message, one home.
_UNWIRED = 'ServiceUnavailableError("authorization service is not available")'


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_walk_sees_the_catalog_source() -> None:
    assert len(_modules()) > 40, f"only {len(_modules())} modules — the walk is not seeing the catalog"


def test_the_injection_seam_still_exists() -> None:
    """Guards the gate: if the dependency is renamed away, the check below must not silently pass."""
    text = _OWNER.read_text()
    assert "FgaClientDep = Annotated[" in text, "api/dependencies.py no longer publishes FgaClientDep"
    assert 'app.state, "fga"' in text, "the one place that knows where the client lives no longer knows"


def test_only_the_dependency_reaches_into_app_state_for_the_fga_client() -> None:
    offences = [
        f"{path.relative_to(_SRC)}:{n}"
        for path in _modules()
        if path not in (_OWNER, _LIFESPAN)
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"app\.state[^\n]*\bfga\b", line) and not line.lstrip().startswith("#")
    ]
    assert not offences, "handlers reaching into app.state for the FGA client — take FgaClientDep instead:\n  " + "\n  ".join(offences)


def test_the_fga_off_or_unwired_preamble_has_one_body() -> None:
    sites = [f"{path.relative_to(_SRC)}:{n}" for path in _modules() for n, line in enumerate(path.read_text().splitlines(), 1) if _UNWIRED in line]
    assert len(sites) <= 1, "the FGA estate-gate preamble is hand-copied — call the shared fga_deps.require_fga:\n  " + "\n  ".join(sites)
