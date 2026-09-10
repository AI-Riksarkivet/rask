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
from collections.abc import Iterator
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


@pytest.fixture(scope="session", autouse=True)
def _unbind_what_this_run_bound() -> Iterator[None]:
    """Remove the namespaces this run BOUND, and say plainly what it could not.

    THE RESIDUE HAS A SOURCE, and it is here. These suites write into the estate's real catalog and
    lineage graph, and nothing removed what they left: § Q8-15 measured 1,163 Dataset nodes and a
    reconcile that fires `storage_loss` and `unreadable` on every 5-minute tick with dead rows in them,
    so a REAL storage loss arrives invisible among them. Retention converges the graph eventually; the
    BINDINGS have no retention at all, which is § Q15-1 — three stale ones hijacking medallion names.

    Autouse and session-scoped so a suite cannot forget it, and `topology.create_top_level` is the one
    door they all bind through.

    WHAT IT WILL NOT DO is force. A namespace still holding tables answers 409 naming them, and that
    refusal is correct — unbinding it would leave real tables unresolvable. Those are reported as a
    warning rather than swallowed or escalated: a cleanup that deletes what a guard refused is worse
    than the residue, and residue nobody is told about is how the graph got to 1,163.
    """
    yield
    from topology import unbind_created

    left = unbind_created()
    if left:
        import warnings

        warnings.warn(
            "e2e left bindings behind (each names why; a 409 means the namespace still holds tables): " + "; ".join(left),
            stacklevel=1,
        )
