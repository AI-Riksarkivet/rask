"""The control root has a SCHEDULED backup, which runs the shipped tool against the control root the catalog writes.

[[LH-110]]. The control root is the registry holding every project, warehouse, namespace binding and
bucket claim. Lose it and the Lance data survives while nothing can route to it — the one loss the
lakehouse cannot reconstruct from storage. `scripts/control_root_backup.py` ships backup, verify,
restore and prune, and `docs/runbooks/RUNBOOK-restore.md` documents the drill; this CronJob runs it. How a run
signs is `test_a_backup_run_signs_with_a_session_its_token_is_exchanged_for.py`'s ([[XC-084]]).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.chart_render import PREREQ_ARGS


REPO = Path(__file__).resolve().parents[2]


def _render(*extra: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    cmd += PREREQ_ARGS
    cmd += ["--set-string", "auth.oidc.issuer=http://localhost:8080/dex"]
    cmd += ["--set", "image.localImages=true", *extra]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(done.stderr)
    return [d for d in yaml.load_all(done.stdout, Loader=FAST_LOADER) if d]


_ON = ("--set", "backups.controlRoot.enabled=true")


def _cronjob(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "CronJob" and "control-root-backup" in doc.get("metadata", {}).get("name", ""):
            return doc
    raise AssertionError("no control-root backup CronJob rendered")


def test_enabled_renders_a_cronjob_that_runs_the_shipped_tool() -> None:
    container = _cronjob(_render(*_ON))["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert ("command" in container, container["args"][:3]) == (False, ["python", "/srv/control_root_backup.py", "backup"]), container


def test_the_backup_root_is_the_SAME_expression_the_catalog_calls_its_control_root() -> None:
    """Two spellings of one root is how a backup ends up copying an empty prefix and reporting success."""
    container = _cronjob(_render(*_ON))["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    root = container["args"][container["args"].index("--root") + 1]
    catalog = next(
        e["value"]
        for d in _render(*_ON)
        if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("-catalog")
        for e in d["spec"]["template"]["spec"]["containers"][0].get("env", [])
        if e["name"] == "LANCE_REST_ROOT"
    )

    assert root == catalog, f"the backup reads {root!r} while the catalog writes {catalog!r}"
