"""The gate that renders the PRODUCTION path supplies the credentials that path refuses to default.

[[XC-070]]. `.dagger/charts.go`'s `renderArgs` deliberately renders with a real registry rather than
`image.localImages=true`, and its comment says exactly why: all thirteen chart-render invariants in
`tests/unit/test_invariants.py` take the SIDE-LOAD path, so "the production path, where every image
reference is a real registry address, is rendered by NO test in the estate. This gate is where that
gets covered."

`chart/templates/prod-credentials.yaml` then refuses a real-registry render that still carries
well-known dev credentials — correctly, because "holding the repository is holding the credential".
The two landed independently and are in direct conflict: the gate renders the exact shape the guard
exists to refuse, so `dagger call charts` fails on every invocation. Measured 2026-09-22 against the
real Dagger build: `execution error at (rask/templates/prod-credentials.yaml:73:4)`, four offending
values, exit 1.

A GATE THAT CAN ONLY FAIL IS NOT A GATE. It cannot separate a good chart from a bad one, so its
result stops being read — and the production render path it was built to cover goes back to having no
coverage at all, silently, which is the state this repo's own rule about gates warns about.

THE FIX IS WHAT A REAL DEPLOYMENT DOES: supply the credentials. That keeps the production SHAPE the
gate exists for — a registry-qualified image reference — without asking the guard to make an exception
for a renderer.

THIS TEST TIES THE TWO FILES TOGETHER so they cannot drift apart again: it reads the guard for every
value compared against a well-known literal and requires `renderArgs` to override each. Add a fifth
credential to the guard and this fails until the gate is taught about it.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "chart" / "templates" / "prod-credentials.yaml"
GATE = REPO / ".dagger" / "charts.go"

#: `eq .Values.<path> "<literal>"` — the guard's shape for "this is still the published dev value".
_COMPARED = re.compile(r"eq\s+\.Values\.([A-Za-z0-9_.]+)\s+\"")
#: `if .Values.<path>` with no comparison — a boolean the guard treats as unsafe when true.
_BOOL = re.compile(r"if\s+\.Values\.(openbao\.devMode)\s")


def _refused_values() -> set[str]:
    text = GUARD.read_text()
    return set(_COMPARED.findall(text)) | set(_BOOL.findall(text))


def _render_args() -> str:
    """The `renderArgs` const, joined — it is a multi-line Go string concatenation."""
    text = GATE.read_text()
    start = text.index("const renderArgs =")
    end = text.index("\n\n", start)
    return text[start:end]


def test_the_guard_still_refuses_something() -> None:
    """A regex that matched nothing would make every other leg here vacuous."""
    refused = _refused_values()
    assert len(refused) >= 3, f"the guard was read as refusing {refused}; it names at least the app token, the AGE password and the store key"


def test_the_production_gate_overrides_every_refused_credential() -> None:
    """Exhaustive: ONE unsupplied credential is a gate that fails on every run and is then ignored."""
    args = _render_args()
    missing = sorted(v for v in _refused_values() if f"{v}=" not in args)
    assert not missing, (
        f"`renderArgs` renders the production path but does not supply {missing}, which "
        f"`prod-credentials.yaml` refuses to default there — so `dagger call charts` cannot pass, and "
        "the production render path it exists to cover has no coverage at all"
    )
