"""Shared fixtures for the ingest suite."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest


if TYPE_CHECKING:
    from dapr.ext.workflow import WorkflowActivityContext


@pytest.fixture(autouse=True)
def _local_dir_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `local-dir`'s confinement at pytest's tmp tree for every test.

    `RASK_INGEST_LOCAL_ROOT` has NO default on purpose — unset means `local-dir` is refused, because
    an unconfined root is an arbitrary-file-read primitive aimed at the ingest pod's own filesystem
    (`{"root": "/proc/self", "pattern": "environ"}` lands the process environment, S3 credential and
    all, as rows in a governed table).

    So the suite has to opt in, and it opts in to the ONE tree its fixtures live under. Autouse
    rather than per-test because every local-dir test would otherwise need it and the one that forgot
    would fail confusingly; `tmp_path_factory.getbasetemp()` is the parent of every test's
    `tmp_path`, so a test's own directory is always inside it.
    """
    monkeypatch.setenv("RASK_INGEST_LOCAL_ROOT", str(tmp_path_factory.getbasetemp()))


#: The context a Dapr activity is CALLED with, for tests that call activities directly.
#:
#: Every activity in this plane takes `ctx: WorkflowActivityContext` as its first parameter because
#: that is the runtime's contract — and NONE of them read it. So tests passed a bare `None`, each
#: site carrying its own `# type: ignore[arg-type]`, and the pile came to 21 of the ty gate's
#: diagnostics: the single largest cause in the plane, and one that made every real signal harder to
#: see.
#:
#: `cast` rather than a fake object, because there is nothing to fake: an activity that never touches
#: `ctx` cannot tell the difference, and constructing a real `WorkflowActivityContext` needs a
#: `durabletask.task.ActivityContext` that exists only inside a running worker. This follows the
#: estate's own precedent — `test_pipelines_registry.py` builds a structural fake and
#: `cast(JobSubmissionClient, ...)`s it so the signature stays honest with zero runtime import.
#:
#: The alternative — widening the activities to `WorkflowActivityContext | None` — was rejected: it
#: weakens a production signature to suit tests, and the parameter is not optional at runtime.
@pytest.fixture
def activity_ctx() -> WorkflowActivityContext:
    """A FIXTURE, not a constant — `--import-mode=importlib` is why.

    Under importlib, test modules are imported as top-level modules with no `sys.path` insertion, so
    `from conftest import ...` raises `ModuleNotFoundError`. pytest still injects conftest FIXTURES
    normally; that is the only channel a shared value can travel here. The first version of this was a
    module constant and every test module failed to collect.
    """
    return cast("WorkflowActivityContext", None)
