"""Shared fixtures for the ingest suite."""

from __future__ import annotations

import pytest


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
