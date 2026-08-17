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

import base64
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import requests


pytestmark = [pytest.mark.e2e, pytest.mark.dummy_lane]

CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "")
ADMIN_TOKEN = os.environ.get("LANCE_E2E_ADMIN_TOKEN", "")
PROJECT = os.environ.get("LANCE_E2E_PROJECT", "acme")
#: The RayService whose ACTIVE cluster runs the job. Not a Deployment name: the chart's Ray runs as a
#: KubeRay RayService, whose head is a pod inside a RayCluster with a generated suffix.
RAY_SERVICE = os.environ.get("LANCE_E2E_RAY_SERVICE", "rask-ray")
#: Where the JOB posts its provenance (in-cluster, from the head pod) and where the TEST reads it
#: back (port-forwarded). Two different addresses for one service, which is the whole reason they
#: are separate knobs: the job cannot reach a localhost forward and the test cannot resolve a
#: cluster DNS name.
LINEAGE_IN_CLUSTER = os.environ.get("LANCE_E2E_LINEAGE_IN_CLUSTER", "http://rask-lineage:8000")
LINEAGE_URL = os.environ.get("LANCE_E2E_LINEAGE_URL", "")

#: The baked entrypoint. A literal on purpose: the whole claim is that this path exists IN THE IMAGE,
#: so deriving it from the same config the image is built against would make the test agree with
#: itself rather than with the cluster.
BAKED_ENTRYPOINT = "python /home/ray/jobs/ray_dummy_job.py"

LANE = "dummy"


def _subject_of(bearer: str) -> str:
    """The `sub` claim, read from the token WITHOUT verifying it.

    Safe here and only here: this is a test deriving the value it will later assert came back, not
    a service making an authorization decision. Nothing is trusted on the strength of it, and the
    token itself is never printed.
    """
    if not bearer or bearer.count(".") != 2:
        return ""
    payload = bearer.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("sub", ""))
    except Exception:  # noqa: BLE001 — a malformed fixture token is a skip, not a crash
        return ""


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


def _head_pod() -> str:
    """The head pod of the RayService's ACTIVE cluster.

    Resolved through `.status.activeServiceStatus.rayClusterName` rather than by grepping for a head
    pod, because during a zero-downtime image upgrade there are TWO ready heads — the outgoing
    cluster and the incoming one — and a selector that matched both would run the job against
    whichever sorted first. That is exactly the window in which someone runs this suite: right after
    deploying a new image, to check the new image.
    """
    kubectl = _kubectl()
    active = subprocess.run(
        [kubectl, "get", "rayservice", RAY_SERVICE, "-o", "jsonpath={.status.activeServiceStatus.rayClusterName}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if active.returncode != 0 or not active.stdout.strip():
        pytest.skip(f"rayservice {RAY_SERVICE} has no active cluster: {active.stderr.strip() or 'no status yet'}")
    pods = subprocess.run(
        [kubectl, "get", "pod", "-l", f"ray.io/cluster={active.stdout.strip()},ray.io/node-type=head", "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if pods.returncode != 0 or not pods.stdout.strip():
        pytest.skip(f"no head pod for active cluster {active.stdout.strip()!r}")
    return pods.stdout.strip()


def _exec_on_head(script: str, *, timeout: int = 300) -> str:
    """Run a Python snippet ON the Ray head — where the cluster's own environment already is.

    The alternative is teaching this suite its own credential path purely to seed a fixture, which
    would then be the thing under test rather than the lane.
    """
    result = subprocess.run(
        [_kubectl(), "exec", _head_pod(), "--", "python", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        pytest.fail(f"head exec failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.stdout


def _submit_on_head(submission_id: str, env_vars: dict[str, str], *, timeout: int = 300) -> str:
    """Submit through the RAY JOBS API — the production path, and the half a direct `python` call
    would skip.

    `--no-wait` on purpose: a blocking submit holds this process for the job's whole runtime, and the
    measured failure was a 10-minute hang. The caller polls the logs instead.
    """
    runtime_env = json.dumps({"env_vars": env_vars})
    result = subprocess.run(
        [
            _kubectl(),
            "exec",
            _head_pod(),
            "--",
            "ray",
            "job",
            "submit",
            "--no-wait",
            "--submission-id",
            submission_id,
            "--runtime-env-json",
            runtime_env,
            "--",
            *BAKED_ENTRYPOINT.split(),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        pytest.fail(f"ray job submit failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.stdout


def _job_logs(submission_id: str, *, timeout: int = 300) -> str:
    return subprocess.run(
        [_kubectl(), "exec", _head_pod(), "--", "ray", "job", "logs", submission_id],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    ).stdout


# --- 1 DECLARED ------------------------------------------------------------------------------------


def test_the_lane_is_DECLARED_through_the_admin_gated_catalog_door(catalog: str) -> None:
    """The record, written through the door a project admin holds — not a chart value."""
    response = requests.post(
        f"{catalog}/v1/project/{PROJECT}/transform/set",
        json={
            "lane": LANE,
            "from_id": f"{PROJECT}-bronze$events",
            "to_id": f"{PROJECT}-silver${LANE}",
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
            "from_id": f"{PROJECT}-bronze$events",
            "to_id": f"{PROJECT}-silver$devmode",
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

#: Seed bronze on the head's local filesystem, with an INCOMPRESSIBLE payload.
#:
#: Incompressible matters: a repetitive fixture (`b"the quick brown fox " * n`) compresses to almost
#: nothing in bronze, so a silver that HAD copied the bytes would still measure small and the
#: zero-copy assertion would pass without earning it. `bytes(range(256))` does not compress.
#:
#: Local rather than S3 deliberately. The cluster's object-store credentials live in the Dapr secret
#: store, and a suite that reached for them would need its own credential path — which then becomes
#: the thing under test. What this proves is the SUBMISSION path and the transform; the S3 half is
#: exercised by the cascade's own suites.
_SEED = r"""
import lance, pyarrow as pa
payload = bytes(range(256)) * 720          # ~184 KB/row, incompressible
rows = pa.table({{"id": pa.array(range(64), pa.int64()), "payload": pa.array([payload] * 64, pa.large_binary())}})
lance.write_dataset(rows, "{bronze}", data_storage_version="2.2", enable_stable_row_ids=True, mode="overwrite")
print("seeded", lance.dataset("{bronze}").count_rows())
"""

_MEASURE = r"""
import json, pathlib, lance
def du(p): return sum(f.stat().st_size for f in pathlib.Path(p).rglob("*") if f.is_file())
sv = lance.dataset("{silver}")
print("RESULT" + json.dumps({{
    "bronze_bytes": du("{bronze}"), "silver_bytes": du("{silver}"),
    "silver_rows": sv.count_rows(), "silver_columns": [f.name for f in sv.schema],
    "silver_version": sv.version,
}}))
"""

_CLEANUP = r"""
import shutil
shutil.rmtree("{base}", ignore_errors=True)
print("cleanup-ok")
"""


@pytest.fixture(scope="module")
def driven() -> Iterator[dict[str, Any]]:
    """Seed bronze, submit the lane TWICE through the Ray Jobs API, and measure both tiers.

    Twice on purpose: the second submission is the replay Dapr is entitled to deliver, and
    merge_insert on the stable id is what makes it converge instead of duplicating. A lane that only
    ever ran once would pass a row-count assertion it has not earned.

    Module-scoped — the cluster round trip is the expensive part, and the assertions below are three
    questions about ONE run, not three runs.
    """
    run = f"{os.getpid()}{uuid.uuid4().hex[:6]}"
    run_id = f"e2e-{run}"
    base = f"/tmp/e2e-dummy-{run}"
    bronze, silver = f"{base}/bronze.lance", f"{base}/silver.lance"

    _exec_on_head(_SEED.format(bronze=bronze))
    # The identity and the provenance target. TO_ID/FROM_ID are CATALOG identifiers, not URIs:
    # the graph node and the FGA object are keyed by `silver$dummy`, and emitting a storage path
    # would name a node no grant matches — every recipient HIDDEN rather than denied.
    env = {
        "FROM_URI": bronze,
        "TO_URI": silver,
        "RUN_ID": run_id,
        "TO_ID": f"{PROJECT}-silver$dummy",
        "FROM_ID": f"{PROJECT}-bronze$events",
        "PROJECT": PROJECT,
        "ORIGINATOR": _subject_of(ADMIN_TOKEN),
        "LINEAGE_URL": LINEAGE_IN_CLUSTER,
        "LINEAGE_TOKEN": ADMIN_TOKEN,
    }
    logs = []
    for attempt in ("first", "replay"):
        submission = f"e2e-dummy-{run}-{attempt}"
        _submit_on_head(submission, env)
        for _ in range(30):
            text = _job_logs(submission)
            if "rows_written" in text or "Traceback" in text:
                break
            time.sleep(4)
        else:
            pytest.fail(f"submission {submission} produced no result within the poll window")
        logs.append(text)

    marker = _exec_on_head(_MEASURE.format(bronze=bronze, silver=silver))
    at = marker.rfind("RESULT")
    if at < 0:
        pytest.fail(f"the measure step produced no RESULT line:\n{marker}")
    result: dict[str, Any] = json.loads(marker[at + len("RESULT") :])
    result["first_log"], result["replay_log"] = logs[0], logs[1]
    result["run_id"] = run_id
    yield result
    _exec_on_head(_CLEANUP.format(base=base), timeout=120)


def test_the_BAKED_entrypoint_exists_in_the_deployed_image_and_runs(driven: dict[str, Any]) -> None:
    """The exit-2 class, caught by exercising it rather than by reading a dockerfile.

    `scripts/ray_dummy_job.py` was baked into NO image until 2026-08-17 — it existed, it was
    referenced by nothing, and every attempt to run this lane would have died with
    "can't open file" and exit code 2.
    """
    log = driven["first_log"]
    assert "can't open file" not in log, f"the entrypoint is not in the deployed image:\n{log[-800:]}"
    assert "Traceback" not in log, f"the baked entrypoint raised:\n{log[-800:]}"
    assert '"rows_written": 64' in log, f"the job did not report 64 written rows:\n{log[-800:]}"
    assert driven["silver_rows"] == 64


def test_silver_carries_a_REFERENCE_not_the_payload_bytes(driven: dict[str, Any]) -> None:
    """Condition 6, measured rather than asserted by schema alone.

    The column check says the design is right; the byte check says the implementation is. A silver
    that grew like bronze copied the payload no matter what its schema claims.
    """
    assert "source_rowid" in driven["silver_columns"], driven["silver_columns"]
    assert "payload" not in driven["silver_columns"], "silver must not carry the bronze payload column"

    bronze_bytes, silver_bytes = int(driven["bronze_bytes"]), int(driven["silver_bytes"])
    assert bronze_bytes > 100_000, f"the fixture did not write a meaningful payload ({bronze_bytes} B)"
    assert silver_bytes * 10 < bronze_bytes, (
        f"silver is {silver_bytes} B against bronze's {bronze_bytes} B — that is a COPY, not a pointer. A tier transition must move zero payload bytes."
    )


def test_a_REPLAYED_run_converges_rather_than_duplicating(driven: dict[str, Any]) -> None:
    """Condition 4's redelivery half: merge_insert on the stable id, not append.

    Dapr redelivers, and it is right to — the contract is that a handler may run twice. What must
    not happen is 128 rows from 64 inputs.
    """
    assert "Traceback" not in driven["replay_log"], driven["replay_log"][-800:]
    assert driven["silver_rows"] == 64, f"the replay duplicated: {driven['silver_rows']} rows from 64 bronze rows"
    assert driven["silver_version"] >= 2, "the replay did not commit a second version — it may not have run at all"


# --- 5 LINEAGE ------------------------------------------------------------------------------------


def test_the_run_emits_a_TERMINAL_event_that_READS_BACK_from_the_lineage_service(driven: dict[str, Any]) -> None:
    """Condition 5, asserted on the DELIVERED ROW rather than on the emit's acknowledgement.

    That distinction is the whole point. An event the plane cannot target is answered with a SUCCESS
    ack and then discarded, so a producer asserting "the POST returned 2xx" proves only that it was
    accepted — never that anything is recoverable, and never that a person could be told. The lane
    emitted nothing at all until 2026-08-17 while its docstring advertised `LINEAGE_JSON`, and no
    ack-shaped assertion anywhere would have noticed.

    Four fields, because `notifiable()` drops the event on ANY miss and says nothing:
    terminal state, a targetable principal, `lance.project`, and an output named as the FGA object.
    """
    if not LINEAGE_URL:
        pytest.skip("set LANCE_E2E_LINEAGE_URL to the port-forwarded lineage service to read events back")

    response = requests.get(f"{LINEAGE_URL.rstrip('/')}/events?limit=200", headers=_headers(), timeout=30)
    if response.status_code in (401, 403):
        pytest.skip(f"the lineage feed is governed and this token cannot read it ({response.status_code})")
    assert response.status_code == 200, response.text

    events = response.json().get("events", [])
    mine = [e for e in events if e.get("job", "").endswith(f"{PROJECT}-silver$dummy") or driven["run_id"] in json.dumps(e)]

    if not mine:
        # TWO different failures wear one symptom here, and they need opposite answers. The lane
        # emitting NOTHING is a defect in the lane. The lane emitting and being REFUSED is a missing
        # deployment grant — the FGA prerequisite every new lineage producer has to ship. Only the
        # job's own stderr can tell them apart, which is why `emit()` prints the HTTP status.
        refused = "lineage-emit-failed" in driven["first_log"] and "status=403" in driven["first_log"]
        assert not refused, (
            "the lane EMITTED and the ingest REFUSED it (403 can_write_data on the output).\n\n"
            "This is a missing GRANT, not a broken lane, and the estate already ships the fix:\n"
            f"    scripts/seed_medallion_fga.sh {PROJECT} <zone-warehouse-id>\n\n"
            "A tenant cascade targets the project-QUALIFIED namespaces, which inherit NOTHING from the "
            "estate seed — the movers are correctly denied and the trigger dead-letters (fail-closed). "
            "That script writes the three groups: the `<p>-<stage>` namespaces parented under the "
            "tenant's zone warehouse, the mover rungs on the qualified stages, and the table->namespace "
            "parent links.\n\n"
            f"This lane needs one link the script does not know about: namespace:{PROJECT}-silver -> "
            f"table:{PROJECT}-silver$dummy (it seeds $features, the HTR lane's output)."
        )
        pytest.fail(f"the run emitted nothing readable: {driven['run_id']} absent from {len(events)} events")

    terminal = [e for e in mine if e.get("event_type") in ("COMPLETE", "FAIL")]
    assert terminal, f"no TERMINAL event for this run — START/RUNNING notify nobody: {mine[:2]}"

    row = terminal[-1]
    assert row["event_type"] == "COMPLETE", f"the run did not complete: {row}"
    assert row.get("outputs"), "an output-less run names no object and is refused by the plane"
    assert f"{PROJECT}-silver$dummy" in row["outputs"], f"the output must be named as the FGA object is: {row['outputs']}"
