"""A service conftest that mutates `os.environ` at import must hand it back.

A fleet service builds its app at MODULE level (`app = make_service_app(...)`), so settings like
`RASK_API_PREFIX` are read once, at import, and a test directory that needs a different value has to
set it before that import. That much is unavoidable and two conftests do it.

THE WINDOW IS THE IMPORT, AND ONLY THE IMPORT. pytest collects every module in every testpath before
running a single test, so an environment variable set and left set is set for all 21 testpaths — and
the damage lands on whoever is imported next rather than on the conftest that did it. Measured
2026-09-22: `services/controlplane/tests/conftest.py` set `RASK_API_PREFIX=/api` and walked away;
`services/compute/tests/conftest.py` then `setdefault`s `/api/v1`, which is a NO-OP against an
already-set value, so compute's app mounted at `/api` while its tests requested `/api/v1` and EIGHT
of them 404'd. `pytest services/controlplane/tests services/compute/tests` failed 8; either directory
alone passed. `make test` escaped it only because `testpaths` happens to list compute first, which
`pytest-randomly` is free to undo.

WHAT THIS GATE CANNOT SEE, stated so it is not mistaken for more: it reads the source rather than
running the collection, so it catches a conftest that never restores, not one whose restore is wrong.
The behavioural proof is running two such directories together in both orders.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]

#: Writes to the process environment. `setdefault` counts: it is still a mutation that outlives the
#: import, and it is the one that caused the measured failure.
#:
#: LEADING WHITESPACE ALLOWED, because a conftest that loops over a dict of variables indents the write
#: — and an anchored `^os\.environ` silently skipped exactly that file, leaving the gate green over the
#: one conftest it was written for. Found by mutation-checking it rather than by it going red.
_MUTATES = re.compile(r"^\s*os\.environ(?:\[[^\]]+\]\s*=|\.setdefault\()", re.MULTILINE)
#: A restore: the saved value is put back, or the key removed when it had none.
_RESTORES = re.compile(r"os\.environ\.pop\(|os\.environ\[[^\]]+\]\s*=\s*_prev", re.MULTILINE)


def _mutating_conftests() -> list[Path]:
    return [p for p in REPO.glob("services/*/tests/conftest.py") if _MUTATES.search(p.read_text(encoding="utf-8"))]


def test_every_conftest_that_sets_an_env_var_also_restores_it() -> None:
    mutating = _mutating_conftests()
    assert mutating, "no service conftest mutates os.environ — this gate would pass vacuously"

    unrestored = sorted(str(p.relative_to(REPO)) for p in mutating if not _RESTORES.search(p.read_text(encoding="utf-8")))
    assert not unrestored, (
        f"these conftests set an environment variable at import and never hand it back: {unrestored}. "
        f"pytest collects every testpath before running anything, so the value is in force for the "
        f"whole session and breaks whichever service is imported next — see this file's docstring for "
        f"the measured case. Save the previous values, import the package inside that window, restore."
    )
