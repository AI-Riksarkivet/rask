"""Force the `e2e` marker onto everything collected under tests/e2e-py.

These are LIVE suites — they need a deployed stack (kind/k3s, port-forwards, seeded
grants) and must never run in an offline `make test`. The root pyproject collects this
directory (so the suites can't silently vanish from the gate — see
tests/unit/test_e2e_collection_gate.py) and offline runs deselect with `-m "not e2e"`.

Per-file `pytestmark = pytest.mark.e2e` is the convention, but one forgotten file
(it has happened: test_user_state_e2e.py carried only its per-suite marker) would
make an offline run hit a live endpoint. Location decides, so enforce by location.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING


# This directory on `sys.path`, so a flat helper beside a suite is importable BY NAME.
#
# Needed because the repo runs `--import-mode=importlib`: a suite is imported from a path that is on
# no default sys.path, and `tests/e2e-py` is not a legal package name (the hyphen), so a sibling
# module cannot be reached as `from .helper import x` or `tests.e2e_py.helper`. The CAS suite needs
# one — its ProcessPoolExecutor spawns, and a spawned child can only unpickle a worker whose module it
# can import; see cas_append_worker.
sys.path.insert(0, str(Path(__file__).parent))

import pytest


if TYPE_CHECKING:
    from byo_scenario import ScenarioRun, Tenant


_HERE = Path(__file__).parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark everything here `e2e`, and deselect every `chaos` drill unless the run asks for them with `--chaos`.

    A drill takes down a dependency of the release it runs against, so a drive of the deployed release runs one only when
    its caller says so (XC-095). Deselected rather than skipped: a skip would fail `--require-live` on a drive that was
    never meant to run them.
    """
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.e2e)
    if config.getoption("--chaos"):
        return
    drills = [item for item in items if item.get_closest_marker("chaos")]
    if drills:
        config.hook.pytest_deselected(items=drills)
        items[:] = [item for item in items if not item.get_closest_marker("chaos")]


# `--require-live` lives in its own module so it can be exercised without this file's live-estate
# cleanup fixture; its hooks are called from here because pytest collects hooks from the conftest it loads.
from require_live import pytest_addoption as _require_live_option  # noqa: E402 — after the sys.path insert above
from require_live import pytest_sessionfinish  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    _require_live_option(parser)
    parser.addoption(
        "--chaos",
        action="store_true",
        default=False,
        help="run the chaos drills, which take down the dependencies of the release they run against",
    )


__all__ = ["pytest_addoption", "pytest_sessionfinish"]


@pytest.fixture(scope="session")
def byo_tenant() -> Iterator[Tenant]:
    """The scratch tenant the bring-your-own scenario runs on (`byo_scenario`), made once and removed at session end.

    Imported here rather than at the top: an offline collection of this directory loads this file, and it must not
    load a Lance client to collect suites it then deselects.
    """
    from byo_scenario import Tenant

    made = Tenant()
    try:
        made.create()
        yield made
    finally:
        left = made.tear_down()
        assert not left, f"the scratch tenant {made.project} was not fully removed: {left}"


@pytest.fixture(scope="session")
def byo_run(byo_tenant: Tenant) -> ScenarioRun:
    """One drive of the scenario on the session's tenant, read by every module that asserts on it."""
    from byo_scenario import drive

    return drive(byo_tenant)


@pytest.fixture(scope="session", autouse=True)
def _unbind_what_this_run_bound() -> Iterator[None]:
    """Remove the namespaces this run BOUND, and say plainly what it could not.

    THE RESIDUE HAS A SOURCE, and it is here. These suites write into the estate's real catalog, and
    nothing removed what they left. Lineage's feed converges by retention; the BINDINGS have no
    retention at all, which is § Q15-1 — three stale ones hijacking medallion names.

    Autouse and session-scoped so a suite cannot forget it, and `topology.create_top_level` is the one
    door they all bind through.

    WHAT IT WILL NOT DO is force. A namespace still holding tables answers 409 naming them, and that
    refusal is correct — unbinding it would leave real tables unresolvable. Those are reported as a
    warning rather than swallowed or escalated: a cleanup that deletes what a guard refused is worse
    than the residue, and residue nobody is told about grows unseen.
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
