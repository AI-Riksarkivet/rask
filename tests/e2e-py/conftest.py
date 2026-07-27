"""Force the `e2e` marker onto everything collected under tests/e2e-py.

These are LIVE suites — they need a deployed stack (kind/k3s, port-forwards, seeded
grants) and must never run in an offline `make test`. The root pyproject collects this
directory (so the suites can't silently vanish from the gate — see
tests/unit/test_e2e_collection_gate.py) and offline runs deselect with `-m "not e2e"`.

Per-file `pytestmark = pytest.mark.e2e` is the convention, but one forgotten file
(it has happened: test_user_state_e2e.py carried only its per-suite marker) would
make an offline run hit a live endpoint. Location decides, so enforce by location.
"""

from pathlib import Path

import pytest


_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.e2e)
