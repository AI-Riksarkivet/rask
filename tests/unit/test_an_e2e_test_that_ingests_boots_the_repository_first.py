"""An e2e test that writes lineage must first provision the storage that write needs.

`ingest_event` writes the AGE graph AND a row in `public.lineage_events`, and the feed table is
created at BOOT by the lifespan, never on first write. Ten tests in `test_lineage_e2e.py` hand-rolled
`LineageRepository(pool, "lineage")`; two called `ensure_events_table` because they are ABOUT that
call, and the rest inherited a table from whichever test ran before them. The result was a suite
sorted by line number: the three tests above those two failed `UndefinedTable` and the five below
them passed, against one healthy database and one defect.

THE ORDER-DEPENDENCE IS THE POINT, not the three red tests. It was introduced the day `ingest_event`
started writing a feed row, stayed invisible for as long as the lane was down, and would have
survived the fix if the fix were "call `ensure_events_table` in the three that failed" — the next
test written above them fails again. So the boot sequence lives in `tests/e2e-py/lineage_boot.py`
and this gate holds two things: every ingesting test goes through it, and it still boots what the
service's own lifespan boots.

A test that is ABOUT a boot step is exempt by construction rather than by a name list: it calls that
step itself, which is exactly what this gate asks for.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_SUITE = _ROOT / "tests" / "e2e-py"
_HELPER = _SUITE / "lineage_boot.py"
_MAIN = _ROOT / "services" / "lineage" / "src" / "lineage" / "main.py"
#: Either satisfies the rule: the shared helper, or the boot step itself for a test that exercises it.
_BOOTS = ("booted_repository(", "ensure_events_table(")


def _ingesting_functions() -> list[tuple[str, str, str]]:
    """`(file, function, source)` for every top-level e2e test function that calls `ingest_event`."""
    out: list[tuple[str, str, str]] = []
    for path in sorted(_SUITE.glob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        if "ingest_event(" not in src:
            continue
        for node in ast.parse(src).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seg = ast.get_source_segment(src, node) or ""
            if "ingest_event(" in seg:
                out.append((path.name, node.name, seg))
    return out


def test_the_scan_finds_the_ingesting_tests() -> None:
    """Anti-vacuity: the parametrisation below is empty if the glob, the parse or the suite moves,
    and an empty parametrisation is a gate that reports coverage it does not have."""
    found = _ingesting_functions()

    assert len(found) >= 8, f"only {len(found)} ingesting e2e tests found — the scan is broken, not the suite"


@pytest.mark.parametrize(("file", "name"), [(f, n) for f, n, _ in _ingesting_functions()])
def test_an_ingesting_test_boots_its_repository(file: str, name: str) -> None:
    source = next(s for f, n, s in _ingesting_functions() if (f, n) == (file, name))

    assert any(call in source for call in _BOOTS), (
        f"{file}::{name} calls ingest_event on a repository it never booted. It passes only while some "
        f"earlier test in the file happens to have created public.lineage_events; run it first, alone, or "
        f"in parallel and it dies UndefinedTable. Build it with `booted_repository(pool)`."
    )


def test_the_helper_boots_everything_the_lifespan_boots() -> None:
    """The anti-drift clause: a step added to the service's boot must reach the suite by being added
    to one helper, not by being remembered in each of ten tests."""
    lifespan = set(re.findall(r"await repository\.(ensure_\w+)\(", _MAIN.read_text(encoding="utf-8")))
    helper = set(re.findall(r"await repository\.(ensure_\w+)\(", _HELPER.read_text(encoding="utf-8")))

    assert lifespan, "no ensure_* boot steps parsed out of the lineage lifespan — the parse moved, not the code"
    assert lifespan <= helper, f"the lifespan boots steps the e2e helper does not: {sorted(lifespan - helper)}"
