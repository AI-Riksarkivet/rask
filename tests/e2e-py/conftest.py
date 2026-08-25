"""Force the `e2e` marker onto everything collected under tests/e2e-py.

These are LIVE suites — they need a deployed stack (kind/k3s, port-forwards, seeded
grants) and must never run in an offline `make test`. The root pyproject collects this
directory (so the suites can't silently vanish from the gate — see
tests/unit/test_e2e_collection_gate.py) and offline runs deselect with `-m "not e2e"`.

Per-file `pytestmark = pytest.mark.e2e` is the convention, but one forgotten file
(it has happened: test_user_state_e2e.py carried only its per-suite marker) would
make an offline run hit a live endpoint. Location decides, so enforce by location.
"""

import sys
from pathlib import Path


# This directory on `sys.path`, so a flat helper beside a suite is importable BY NAME.
#
# Needed because the repo runs `--import-mode=importlib`: a suite is imported from a path that is on
# no default sys.path, and `tests/e2e-py` is not a legal package name (the hyphen), so a sibling
# module cannot be reached as `from .helper import x` or `tests.e2e_py.helper`. The CAS suite needs
# one — its ProcessPoolExecutor spawns, and a spawned child can only unpickle a worker whose module it
# can import; see cas_append_worker.
sys.path.insert(0, str(Path(__file__).parent))

import pytest


_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.e2e)
