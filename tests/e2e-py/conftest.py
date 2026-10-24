"""Force the `e2e` marker onto everything collected under tests/e2e-py.

These are LIVE suites — they need a deployed stack (kind/k3s, port-forwards, seeded
grants) and must never run in an offline `make test`. The root pyproject collects this
directory (so the suites can't silently vanish from the gate — see
tests/unit/test_e2e_collection_gate.py) and offline runs deselect with `-m "not e2e"`.

Per-file `pytestmark = pytest.mark.e2e` is the convention, but one forgotten file
(a suite carrying only its per-suite marker) would make an offline run hit a live
endpoint. Location decides, so enforce by location.
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
    """Mark everything here `e2e`; deselect every `chaos` drill unless the run asks for them with `--chaos`, and the soak
    unless the run names the drive it reads with `--soak`; and run the drills after every other test the run selected.

    A drill takes down a dependency of the release it runs against, so a drive of the deployed release runs one only when
    its caller says so (XC-095), and runs it last, so no other test reads an estate a drill has just had down. The soak
    reads a drive an earlier run kept (`kept_drive`), a day on. Deselected rather than skipped: a skip would fail
    `--require-live` on a drive that was never meant to run them.
    """
    for item in items:
        if _HERE in item.path.parents:
            item.add_marker(pytest.mark.e2e)
    asked = {"chaos": config.getoption("--chaos"), "soak": config.getoption("--soak") is not None}

    def unasked(item: pytest.Item) -> bool:
        return any(item.get_closest_marker(marker) and not wanted for marker, wanted in asked.items())

    held = [item for item in items if unasked(item)]
    if held:
        config.hook.pytest_deselected(items=held)
        items[:] = [item for item in items if not unasked(item)]
    items.sort(key=lambda item: item.get_closest_marker("chaos") is not None)


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
    parser.addoption(
        "--keep-drive",
        metavar="PATH",
        default=None,
        help="keep the bring-your-own scenario's tenant past a session whose every test passed, and record its drive at PATH for the soak",
    )
    parser.addoption(
        "--soak",
        metavar="PATH",
        default=None,
        help="run the soak over the drive kept at PATH (--keep-drive), and remove its tenant once the soak passes",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to keep a drive where one is already kept: its record is the only thing that names its tenant."""
    keep_at = config.getoption("--keep-drive")
    if keep_at is not None and Path(keep_at).exists():
        raise pytest.UsageError(f"a drive is already kept at {keep_at}: soak it with `--soak {keep_at}`, or remove its tenant and the record first")


__all__ = ["pytest_addoption", "pytest_sessionfinish"]


@pytest.fixture(scope="session")
def byo_tenant() -> Iterator[Tenant]:
    """The scratch tenant the bring-your-own scenario runs on (`byo_scenario`), made once and removed at session end,
    unless the session keeps its drive for the soak (`byo_run`).

    Imported here rather than at the top: an offline collection of this directory loads this file, and it must not
    load a Lance client to collect suites it then deselects.
    """
    from byo_scenario import Tenant

    made = Tenant()
    try:
        made.create()
        yield made
    finally:
        if not made.kept:
            left = made.tear_down()
            assert not left, f"the scratch tenant {made.project} was not fully removed: {left}"


@pytest.fixture(scope="session")
def byo_run(request: pytest.FixtureRequest, byo_tenant: Tenant) -> Iterator[ScenarioRun]:
    """One drive of the scenario on the session's tenant, read by every module that asserts on it. With `--keep-drive`,
    a session whose every test passed keeps the tenant and records the drive for the soak (`kept_drive`); torn down
    before the tenant, so the tenant's own teardown sees it kept."""
    from byo_scenario import drive
    from kept_drive import keep, passed_whole

    run = drive(byo_tenant)
    yield run
    keep_at = request.config.getoption("--keep-drive")
    if keep_at is not None and passed_whole(request.session):
        keep(byo_tenant, run, Path(keep_at))


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
