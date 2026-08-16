"""#53 Ray BATCH path — the distributed-Lance capability job on a real KubeRay cluster.

Submits ``scripts/ray_lance_job.py`` to the deployed ray-lance-demo cluster and asserts its four
distributed effects observably (write at 2.2 + stable row ids, a scalar index that serves a query,
schema evolution, compaction), so the "distributed Lance write/index/evolve/compact on real Ray"
claim can never silently regress. Distributed where the lance_ray↔pylance versions align, native
fallback (documented) where they do not.

This job writes throwaway ``ray-<run>/`` demo datasets and emits NO lineage by design — governed
batch provenance lives in the medallion Ray stage path (``ray_stage_job.py``), covered by the
governed-union suite. See docs/RAY.md.

"Throwaway" is now TRUE. Nothing threw them away until 2026-08-16, and they land in
``s3://lance-catalog/`` — the PRIMARY governed bucket the maintenance sweep walks every 120s. Each
run mints a fresh ``ray-e2ebatch<pid>/{src,out}`` pair and left it there forever, so the residue
accumulated silently: measured live, **32 of 45 datasets in that bucket were this suite's leftovers**
— every one of them opened by every sweep tick, for good. The test now deletes its own prefix in a
``finally``, so a failed assertion cleans up too (a failing test is exactly when a stray dataset is
most likely, and least likely to be noticed).

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


#: Deletes one run's ``ray-<run>/`` prefix from the bucket the job hardcodes. Runs ON the Ray head
#: because that is where the credentials already are (S3_ENDPOINT/S3_KEY/S3_SECRET are on its pod
#: spec, and `ray_lance_job.py` reads exactly those) — the alternative would be teaching this suite a
#: second credential path for the sole purpose of cleaning up after the first.
_CLEANUP = """
import os, pyarrow.fs as pafs
fs = pafs.S3FileSystem(
    endpoint_override=os.environ["S3_ENDPOINT"],
    access_key=os.environ["S3_KEY"],
    secret_key=os.environ["S3_SECRET"],
    region=os.environ.get("S3_REGION", "us-east-1"),
    scheme="http",
)
p = "lance-catalog/ray-{run}"
try:
    fs.delete_dir_contents(p, missing_dir_ok=True)
    fs.delete_dir(p)
except Exception as exc:
    print("cleanup-failed", exc)
else:
    print("cleanup-ok", p)
"""


def _cleanup(kubectl: str, run: str) -> None:
    """Best-effort removal of this run's datasets — never fails the test.

    Best-effort deliberately: a cleanup that can fail the run would turn "the object store hiccuped"
    into "distributed Lance regressed", which is the opposite of what this suite exists to report. A
    leaked prefix is visible in the sweep's own dataset count; a false red is not.
    """
    subprocess.run(
        [kubectl, "exec", RAY_HEAD_DEPLOY, "--", "python", "-c", _CLEANUP.format(run=run)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "UPSTREAM lance-ray 0.5.0: `lance_ray.compact_files` does not reduce fragments. The job's first "
        "three stages pass on the real cluster (distributed WRITE 4 fragments, distributed INDEX, schema "
        "EVOLVE 3->4); stage 4 reports `compaction did not reduce fragments: 4->4` and exits 1. Measured "
        "2026-08-16: NATIVE pylance 10.0.0 `optimize.compact_files(target_rows_per_fragment=32)` on the "
        "identical shape (64 rows / 4 fragments of 16 / dsv 2.2 / stable row ids / post-`add_columns`) "
        "reduces 4 -> 2, so Lance is fine and the distributed path is not. The call matches lance-ray "
        "0.5.0's signature, so this is not misuse. Kept STRICT on purpose: rewriting the job to use native "
        "compaction would make the suite green by deleting the very capability it exists to prove, and a "
        "strict xfail goes red the moment upstream fixes this — which is the signal to remove this marker."
    ),
)
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
    try:
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
    finally:
        # In a `finally`, not after the asserts: a FAILED run leaves datasets too, and is precisely when
        # nobody goes back to tidy up. The prefix is minted per-pid, so this removes only this run's.
        _cleanup(kubectl, run)
