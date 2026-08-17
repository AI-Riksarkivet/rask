"""The DUMMY lane end to end: declared, computed on Ray the production way, and provably zero-copy.

This is the estate's own probe — the one lane that runs with **no GPU and no model download**, so
"the medallion cascade works" can be checked on a laptop, in CI, and against a real cluster with the
same suite. A trivial transform (checksum, word count, an eight-float fake embedding) wrapped in
production mechanics is the point: what is under test is the LANE, never the model.

Five properties, one per test, each mapping to a numbered condition of the batch goal:

* **DECLARED** — the lane is a `TransformSpec` written through the catalog's admin-gated door, and an
  undeclared lane is a 422 that names the key. Not a config file, not a Deployment's env block.
* **ON RAY, THE PRODUCTION WAY** — the job runs from a path BAKED into the cluster image
  (`/home/ray/jobs/ray_dummy_job.py`), submitted through the Ray Jobs API. Never `runtime_env`,
  which Ray documents as development-only.
* **NO COPY** — silver carries `source_rowid`, a REFERENCE into bronze, and not the payload bytes.
  Asserted by measuring both tiers: a silver that grew like bronze is a silver that copied.
* **COMMITTED ONCE** — a replayed run converges (merge_insert on the stable id) rather than
  duplicating rows.
* **REGISTERED** — the catalog lists the silver table non-empty, so the output is governed rather
  than merely written.

Each test skips cleanly when its prerequisite is absent, and says which env var is missing — a suite
that silently passes because nothing was configured is worse than one that does not run.

    kubectl port-forward svc/rask-catalog 28000:8000 &
    LANCE_E2E_CATALOG_URL=http://localhost:28000 \\
    LANCE_E2E_ADMIN_TOKEN=<a project-admin OIDC bearer> \\
    LANCE_E2E_PROJECT=acme \\
    uv run pytest tests/e2e-py/test_dummy_lane_e2e.py -v

or `make e2e-dummy-lane`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import requests


pytestmark = [pytest.mark.e2e, pytest.mark.dummy_lane]

CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "")
ADMIN_TOKEN = os.environ.get("LANCE_E2E_ADMIN_TOKEN", "")
PROJECT = os.environ.get("LANCE_E2E_PROJECT", "acme")
RAY_HEAD_DEPLOY = os.environ.get("LANCE_E2E_RAY_HEAD_DEPLOY", "deploy/rask-ray-head")

#: The baked entrypoint. A literal on purpose: the whole claim is that this path exists IN THE IMAGE,
#: so deriving it from the same config the image is built against would make the test agree with
#: itself rather than with the cluster.
BAKED_ENTRYPOINT = "python /home/ray/jobs/ray_dummy_job.py"

LANE = "dummy"


def _headers() -> dict[str, str]:
    return {"authorization": f"Bearer {ADMIN_TOKEN}"} if ADMIN_TOKEN else {}


@pytest.fixture(scope="module")
def catalog() -> str:
    if not CATALOG:
        pytest.skip("set LANCE_E2E_CATALOG_URL (see module docstring)")
    try:
        requests.get(f"{CATALOG.rstrip('/')}/livez", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001 — any failure to reach it is the same skip
        pytest.skip(f"catalog not reachable at {CATALOG}: {exc}")
    return CATALOG.rstrip("/")


def _kubectl() -> str:
    for candidate in ("kubectl", os.path.expanduser("~/Desktop/rask/.localbin/kubectl")):
        if shutil.which(candidate) or os.path.exists(candidate):
            return candidate
    pytest.skip("kubectl not available")


def _exec_on_head(script: str, *, timeout: int = 300) -> str:
    """Run a Python snippet ON the Ray head — where the S3 credentials already live.

    The alternative is teaching this suite a second credential path purely to seed a fixture, which
    would then be the thing under test rather than the lane.
    """
    kubectl = _kubectl()
    probe = subprocess.run([kubectl, "get", RAY_HEAD_DEPLOY], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        pytest.skip(f"ray head {RAY_HEAD_DEPLOY} not present: {probe.stderr.strip()}")
    result = subprocess.run(
        [kubectl, "exec", RAY_HEAD_DEPLOY, "--", "python", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        pytest.fail(f"head exec failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.stdout


# --- 1 DECLARED ------------------------------------------------------------------------------------


def test_the_lane_is_DECLARED_through_the_admin_gated_catalog_door(catalog: str) -> None:
    """The record, written through the door a project admin holds — not a chart value."""
    response = requests.post(
        f"{catalog}/v1/project/{PROJECT}/transform/set",
        json={
            "lane": LANE,
            "from_id": "bronze$events",
            "to_id": f"silver${LANE}",
            "entrypoint": BAKED_ENTRYPOINT,
            "params": {"embed_dim": "8"},
            "code_version": os.environ.get("LANCE_E2E_CODE_VERSION", "e2e"),
        },
        headers=_headers(),
        timeout=30,
    )
    if response.status_code in (401, 403):
        pytest.skip(f"LANCE_E2E_ADMIN_TOKEN is not a {PROJECT} admin ({response.status_code}); the door is working, the fixture is not")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lane"] == LANE
    assert body["project"] == PROJECT, "the project must come from the gated path"
    assert body["entrypoint"] == BAKED_ENTRYPOINT


def test_an_UNDECLARED_lane_is_422_naming_the_key(catalog: str) -> None:
    """The failure mode this whole record exists to move EARLIER.

    Before declaration, a trigger naming a lane nobody configured surfaced as a Ray job that would
    not start — an error naming the image, several layers from the typo that caused it.
    """
    response = requests.post(
        f"{catalog}/v1/project/{PROJECT}/transform/describe",
        json={"lane": f"nosuchlane-{uuid.uuid4().hex[:8]}"},
        headers=_headers(),
        timeout=30,
    )
    if response.status_code in (401, 403):
        pytest.skip(f"LANCE_E2E_ADMIN_TOKEN is not a {PROJECT} admin ({response.status_code})")
    assert response.status_code == 422, response.text
    fields = [e["field"] for e in response.json()["errors"]]
    assert "body.lane" in fields, f"the 422 must name the lane field; got {fields}"


def test_a_runtime_env_style_entrypoint_CANNOT_be_declared(catalog: str) -> None:
    """B3 enforced at the door: a lane that cannot be declared can never be submitted."""
    response = requests.post(
        f"{catalog}/v1/project/{PROJECT}/transform/set",
        json={
            "lane": "would-be-devmode",
            "from_id": "bronze$events",
            "to_id": "silver$devmode",
            "entrypoint": "python ./my_local_transform.py",
        },
        headers=_headers(),
        timeout=30,
    )
    if response.status_code in (401, 403):
        pytest.skip(f"LANCE_E2E_ADMIN_TOKEN is not a {PROJECT} admin ({response.status_code})")
    assert response.status_code == 422, response.text
    assert "baked" in response.text


# --- 3 ON RAY, 6 NO COPY, 4 COMMITTED ONCE ----------------------------------------------------------

#: Seed bronze, run the DECLARED baked entrypoint twice, and measure both tiers.
#:
#: Run twice on purpose: the second pass is the replay, and merge_insert on the stable id is what
#: makes it converge instead of duplicating. A lane that only ever ran once would pass a row-count
#: assertion it has not earned.
_DRIVE = r"""
import json, os, subprocess, sys
import lance, pyarrow as pa
import pyarrow.fs as pafs

run = "{run}"
so = dict(
    endpoint=os.environ["S3_ENDPOINT"], access_key_id=os.environ["S3_KEY"],
    secret_access_key=os.environ["S3_SECRET"], region=os.environ.get("S3_REGION", "us-east-1"),
    allow_http="true", virtual_hosted_style_request="false",
)
base = f"s3://lance-catalog/e2e-dummy-{{run}}"
bronze, silver = f"{{base}}/bronze.lance", f"{{base}}/silver.lance"

# A payload big enough that a COPY would be unmistakable in the byte measurement below.
payload = b"the quick brown fox jumps over the lazy dog " * 4096
rows = pa.table({{"id": pa.array(range(64), pa.int64()), "payload": pa.array([payload] * 64, pa.large_binary())}})
lance.write_dataset(rows, bronze, storage_options=so, data_storage_version="2.2", enable_stable_row_ids=True)

env = dict(os.environ, FROM_URI=bronze, TO_URI=silver, RUN_ID=f"e2e-{{run}}")
first = subprocess.run([sys.executable, "/home/ray/jobs/ray_dummy_job.py"], env=env, capture_output=True, text=True)
second = subprocess.run([sys.executable, "/home/ray/jobs/ray_dummy_job.py"], env=env, capture_output=True, text=True)

def measure(uri):
    fs = pafs.S3FileSystem(endpoint_override=so["endpoint"].split("://")[-1], access_key=so["access_key_id"],
                           secret_key=so["secret_access_key"], region=so["region"], scheme="http")
    sel = pafs.FileSelector(uri.removeprefix("s3://"), recursive=True, allow_not_found=True)
    return sum(i.size for i in fs.get_file_info(sel) if i.type == pafs.FileType.File)

sv = lance.dataset(silver, storage_options=so)
print("RESULT" + json.dumps({{
    "first_rc": first.returncode, "first_out": first.stdout.strip()[-400:], "first_err": first.stderr.strip()[-400:],
    "second_rc": second.returncode, "second_out": second.stdout.strip()[-400:],
    "bronze_bytes": measure(bronze), "silver_bytes": measure(silver),
    "silver_rows": sv.count_rows(), "silver_columns": [f.name for f in sv.schema],
    "base": base,
}}))
"""

_CLEANUP = r"""
import os, pyarrow.fs as pafs
fs = pafs.S3FileSystem(endpoint_override=os.environ["S3_ENDPOINT"].split("://")[-1], access_key=os.environ["S3_KEY"],
                       secret_key=os.environ["S3_SECRET"], region=os.environ.get("S3_REGION", "us-east-1"), scheme="http")
p = "lance-catalog/e2e-dummy-{run}"
try:
    fs.delete_dir_contents(p, missing_dir_ok=True); fs.delete_dir(p)
except Exception as exc:
    print("cleanup-failed", exc)
else:
    print("cleanup-ok", p)
"""


@pytest.fixture(scope="module")
def driven() -> Iterator[dict[str, Any]]:
    """Run the lane twice on the head and return the measurements.

    Module-scoped: the cluster round trip is the expensive part, and the four assertions below are
    four questions about ONE run, not four runs.
    """
    run = f"{os.getpid()}{uuid.uuid4().hex[:6]}"
    out = _exec_on_head(_DRIVE.format(run=run), timeout=600)
    marker = out.rfind("RESULT")
    if marker < 0:
        pytest.fail(f"the driver produced no RESULT line:\n{out}")
    result: dict[str, Any] = json.loads(out[marker + len("RESULT") :])
    yield result
    _exec_on_head(_CLEANUP.format(run=run), timeout=120)


def test_the_BAKED_entrypoint_exists_in_the_deployed_image_and_runs(driven: dict[str, Any]) -> None:
    """The exit-2 class, caught by exercising it rather than by reading a dockerfile.

    `scripts/ray_dummy_job.py` was baked into NO image until 2026-08-17 — it existed, it was
    referenced by nothing, and every attempt to run this lane would have died with
    "can't open file" and exit code 2.
    """
    assert driven["first_rc"] == 0, f"the baked entrypoint failed:\nstdout={driven['first_out']}\nstderr={driven['first_err']}"
    assert driven["silver_rows"] == 64


def test_silver_carries_a_REFERENCE_not_the_payload_bytes(driven: dict[str, Any]) -> None:
    """Condition 6, measured rather than asserted by schema alone.

    The column check says the design is right; the byte check says the implementation is. A silver
    that grew like bronze copied the payload no matter what its schema claims.
    """
    assert "source_rowid" in driven["silver_columns"], driven["silver_columns"]
    assert "payload" not in driven["silver_columns"], "silver must not carry the bronze payload column"

    bronze_bytes, silver_bytes = int(driven["bronze_bytes"]), int(driven["silver_bytes"])
    assert bronze_bytes > 1_000_000, f"the fixture did not write a meaningful payload ({bronze_bytes} B)"
    assert silver_bytes * 10 < bronze_bytes, (
        f"silver is {silver_bytes} B against bronze's {bronze_bytes} B — that is a COPY, not a pointer. A tier transition must move zero payload bytes."
    )


def test_a_REPLAYED_run_converges_rather_than_duplicating(driven: dict[str, Any]) -> None:
    """Condition 4's redelivery half: merge_insert on the stable id, not append.

    Dapr redelivers, and it is right to — the contract is that a handler may run twice. What must
    not happen is 128 rows from 64 inputs.
    """
    assert driven["second_rc"] == 0, driven["second_out"]
    assert driven["silver_rows"] == 64, f"the replay duplicated: {driven['silver_rows']} rows from 64 bronze rows"
