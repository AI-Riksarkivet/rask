"""#53 Ray BATCH path — the distributed-Lance capability job on a real KubeRay cluster.

Submits ``scripts/ray_lance_job.py`` to the deployed ray-lance-demo cluster and asserts its four
distributed effects observably (write at 2.2 + stable row ids, a scalar index that serves a query,
schema evolution, compaction), so the "distributed Lance write/index/evolve/compact on real Ray"
claim can never silently regress. Distributed where the lance_ray↔pylance versions align, native
fallback (documented) where they do not.

This job writes throwaway ``ray-<run>/`` demo datasets and emits NO lineage by design — governed
batch provenance lives in the medallion Ray stage path (``ray_stage_job.py``), covered by the
governed-union suite. See docs/RAY.md.

Env: LANCE_E2E_RAY_HEAD_DEPLOY (default deploy/ray-lance-head) — the suite ``kubectl exec``s the head
to run ``ray job submit``; skips cleanly when kubectl or the head is absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


pytestmark = [pytest.mark.e2e, pytest.mark.ray_batch]

RAY_HEAD_DEPLOY = os.environ.get("LANCE_E2E_RAY_HEAD_DEPLOY", "deploy/ray-lance-head")


def _kubectl() -> str:
    for candidate in ("kubectl", os.path.expanduser("~/Desktop/lance-ns/.localbin/kubectl")):
        if shutil.which(candidate) or os.path.exists(candidate):
            return candidate
    pytest.skip("kubectl not available")


def test_batch_job_distributed_write_index_evolve_compact() -> None:
    kubectl = _kubectl()
    # The head must exist and be Ready — this suite does not deploy it (make ray-demo does).
    probe = subprocess.run(
        [kubectl, "get", RAY_HEAD_DEPLOY, "-o", "jsonpath={.status.readyReplicas}"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or (probe.stdout.strip() or "0") == "0":
        pytest.skip(f"{RAY_HEAD_DEPLOY} not ready (run `make ray-demo` first)")

    run = f"e2ebatch{os.getpid()}"
    submit = subprocess.run(
        [
            kubectl,
            "exec",
            RAY_HEAD_DEPLOY,
            "--",
            "ray",
            "job",
            "submit",
            "--address",
            "http://localhost:8265",
            "--runtime-env-json",
            f'{{"env_vars":{{"RUN":"{run}"}}}}',
            "--",
            "python",
            "/home/ray/jobs/ray_lance_job.py",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    out = submit.stdout + submit.stderr
    assert submit.returncode == 0, f"ray job submit failed:\n{out[-2000:]}"

    # The job prints one line per stage with the observable effect — assert each landed, not just that
    # the job exited 0 (a mocked call would exit 0 too).
    assert "stable_row_ids=True" in out and "dsv=2.2" in out, f"write must be 2.2 + stable row ids:\n{out[-800:]}"
    assert "INDEX ok" in out and "row(s)" in out, f"index must build and serve a query:\n{out[-800:]}"
    assert "EVOLVE ok" in out, f"schema evolution must land:\n{out[-800:]}"
    assert "COMPACT ok" in out, f"compaction must run:\n{out[-800:]}"
    assert "RAY-LANCE ALL OK" in out, f"the job must report all four ops succeeded:\n{out[-800:]}"
