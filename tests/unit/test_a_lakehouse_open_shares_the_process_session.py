"""A Lance open on a lakehouse path must be able to share the process's bounded session.

[[LH-096]]. A bare ``lance.dataset(uri)`` mints Lance's default 1 GiB metadata + 6 GiB index caches and
discards them WITH the handle, so on a 512Mi pod the cache never engages and the ceilings dwarf the
limit. A ``lance.Session`` is not a handle cache — its keys are ``(uri, version, etag)``, so a
compaction writes NEW keys and there is no freshness contract to design — which is why sharing one is
the safe half of this row and pinning a handle is not.

MEASURED BY AST 2026-09-16, not by grep: the row's own counts were taken by grep and counted the PROSE,
five of its "bare" hits being docstrings that explain why bare opens are bad. Walking the ASTs, all four
lakehouse services already thread a session at every call — catalog 11, lineage 8, maintenance 9,
medallion 15 — and what remained was six shared helpers in ``service-kit``, which every one of those
services calls.

THE SHARED HELPERS TAKE AN INJECTED SESSION AND CANNOT FETCH THEIR OWN. ``lance_session()`` is
process-wide and int-keyed, but ``shared_lance_session()`` is defined once PER SERVICE from that
service's own caps — so a helper in the platform library has no settings to read and the session has to
arrive from the caller that has one. Passing ``session=None`` is exactly pylance's default, so a caller
that has none is byte-identical to today.

THE EXEMPTIONS ARE NAMED, not implied by the scope. `ingest` is phase 2 and `viewer`/`search` are the
parked zones; listing them here keeps this gate honest about what it does NOT cover, so nobody reads a
green run as "the estate shares one session".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]

#: The phase-1 surface: the four lakehouse services plus the platform library they all call.
_COVERED = ("services/catalog", "services/lineage", "services/medallion", "services/maintenance", "packages/service-kit")

#: Out of scope AND why — phase 2 and the parked zones. A row that closes for the lakehouse must not
#: read as closed for these.
_EXEMPT = {"services/ingest": "phase 2", "services/viewer": "parked zone", "services/search": "parked zone"}


def _bare_opens(area: str) -> list[str]:
    """``file:line`` for every ``lance.dataset(...)`` under ``area`` that passes no ``session``."""
    found: list[str] = []
    for path in sorted((ROOT / area).rglob("*.py")):
        if "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a file that does not parse is another test's problem
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "dataset" or getattr(node.func.value, "id", "") != "lance":
                continue
            if not any(kw.arg == "session" for kw in node.keywords):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


@pytest.mark.parametrize("area", _COVERED)
def test_a_covered_area_opens_lance_through_a_session(area: str) -> None:
    bare = _bare_opens(area)

    assert not bare, (
        f"{area} opens Lance without a session at {bare} — each of those mints Lance's 1 GiB metadata + "
        "6 GiB index ceilings and throws them away with the handle, so the process cache never engages"
    )


def test_the_exempt_areas_are_still_exempt_on_purpose() -> None:
    """If one of these converts, move it into `_COVERED` rather than leaving the exemption to rot.

    Asserted as "still has bare opens" deliberately: a silent exemption over an area that no longer
    needs one is how a gate's stated scope drifts from its real one.
    """
    stale = [area for area in _EXEMPT if not _bare_opens(area)]

    assert not stale, f"these areas no longer open Lance bare, so their exemption is stale — promote them into _COVERED: {stale}"
