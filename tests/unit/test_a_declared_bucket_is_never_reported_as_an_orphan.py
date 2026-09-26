"""A bucket the estate DECLARES must never reach `orphan_buckets`.

That category's contract is that a clean drift report certifies the estate's storage state, and the
trash purge gates on it. So a finding no operator can action holds the total above zero forever and
blocks reclamation with it — the maintenance config's own comment records `rask-observability` doing
exactly that until `minio.buckets` was wired into `MAINTENANCE_S3_PLATFORM_BUCKETS`.

A MULTIBASE DATA BASE IS THE SAME KIND OF THING AND WAS MISSED. `catalog.multibase.dataBases` is an
estate declaration that a bucket exists and may be addressed, and no warehouse record claims it, so
the reconcile called it an orphan. Measured live 2026-09-24: `orphan_buckets: 'lh067-second-store'` —
declared in this repo's own `values-local.yaml` — was one of the two findings behind
`trash_purge_blocked reason="the drift report is NOT clean: 2 finding(s)"`.

BOTH DEPLOYMENTS, because the planner and the worker each carry their own copy of the env and two
copies of a bucket list is how the first one drifted.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_BASE = [
    "--set",
    "image.localImages=true",
    "--set-string",
    "frontend.oidc.sessionSecret=0123456789abcdef0123456789abcdef",
    "--set-string",
    "frontend.oidc.publicIssuer=http://dex.local:5556",
    "--set-string",
    "frontend.oidc.publicOrigin=http://rask.local",
    "--set-string",
    "frontend.oidc.clientSecret=abcdef0123456789abcdef0123456789",
]
_MAINTENANCE = ("rask-maintenance", "rask-maintenance-worker")


def _render(*extra: str) -> list[dict]:
    """The chart rendered with `extra`. A render the chart REFUSES fails the test: skipping it would pass
    every assertion below on a render that never happened."""
    if shutil.which("helm") is None:
        pytest.skip("helm not available")
    done = subprocess.run(["helm", "template", "rask", "chart/", *_BASE, *extra], cwd=_ROOT, capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"the chart refused the render: {done.stderr.strip()[-400:]}"
    return [doc for doc in yaml.safe_load_all(done.stdout) if isinstance(doc, dict)]


def _maintenance_env(docs: list[dict], env_name: str) -> dict[str, str]:
    """The env var `env_name`, verbatim, per maintenance Deployment."""
    out: dict[str, str] = {}
    for doc in docs:
        name = doc.get("metadata", {}).get("name")
        if doc.get("kind") != "Deployment" or name not in _MAINTENANCE:
            continue
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            for env in container.get("env") or []:
                if env["name"] == env_name:
                    out[name] = env.get("value") or ""
    return out


def _platform_buckets(docs: list[dict]) -> dict[str, list[str]]:
    """`MAINTENANCE_S3_PLATFORM_BUCKETS`, split, per maintenance Deployment."""
    return {name: [b for b in value.split(",") if b] for name, value in _maintenance_env(docs, "MAINTENANCE_S3_PLATFORM_BUCKETS").items()}


def test_both_maintenance_deployments_declare_the_set() -> None:
    """Anti-vacuity: the assertions below compare per-deployment, and a missing deployment would make
    them pass on a render that told one half of the service nothing."""
    found = _platform_buckets(_render())
    observability_bucket = yaml.safe_load((_ROOT / "chart" / "values.yaml").read_text())["observability"]["bucket"]

    assert set(found) == set(_MAINTENANCE), f"the platform-bucket env is missing from: {sorted(set(_MAINTENANCE) - set(found))}"
    for name, buckets in found.items():
        assert observability_bucket in buckets, f"{name} does not even name GreptimeDB's bucket, which the default render makes: {buckets}"


@pytest.mark.parametrize("observability", [True, False], ids=["observability-on", "observability-off"])
def test_a_renamed_bucket_is_exempt_under_its_new_name_only(observability: bool) -> None:
    """`orphan_buckets` exempts what the bucket-init Job makes, and the Job makes GreptimeDB's bucket only
    while observability is on. Renamed, so a default spelled a second time shows up as a stray exemption:
    a bucket nothing makes, behind which a real orphan of that name would hide.

    The root needs no entry: maintenance exempts the bucket it sweeps, `MAINTENANCE_S3_BUCKET`, so that
    env must carry the new name. With observability off, a telemetry bucket left on the store is
    reported, because the feature that made it is gone.
    """
    values = yaml.safe_load((_ROOT / "chart" / "values.yaml").read_text())
    spellings = {"root-x", "obs-x", values["minio"]["bucket"], values["observability"]["bucket"]}
    wanted = {"obs-x"} if observability else set()
    docs = _render(
        "--set",
        f"observability.enabled={str(observability).lower()}",
        "--set",
        "minio.bucket=root-x",
        "--set",
        "observability.bucket=obs-x",
        "--set",
        "greptimedb-standalone.objectStorage.s3.bucket=obs-x",
        # The storage registry's observability row is paired too, and this test does not read it.
        "--set",
        "storage.stores=null",
    )
    found = _platform_buckets(docs)

    assert set(found) == set(_MAINTENANCE), f"the platform-bucket env is missing from: {sorted(set(_MAINTENANCE) - set(found))}"
    for name, buckets in found.items():
        assert set(buckets) & spellings == wanted, (
            f"observability.enabled={observability}: {name} exempts {sorted(set(buckets) & spellings)}, wanted {sorted(wanted)}"
        )
    root = _maintenance_env(docs, "MAINTENANCE_S3_BUCKET")
    assert root == dict.fromkeys(_MAINTENANCE, "root-x"), f"the renamed root is not the bucket maintenance sweeps and exempts: {root}"


def test_a_declared_multibase_base_is_a_platform_bucket() -> None:
    """The defect: an opted-in second store is an orphan on every tick and blocks the purge forever."""
    docs = _render("--set", "catalog.multibase.dataBases[0]=s3://second-store/data")

    for name, buckets in _platform_buckets(docs).items():
        assert "second-store" in buckets, (
            f"{name} would report the declared multibase bucket as an orphan: {buckets}. The drift total "
            "then never reaches zero and the trash purge is blocked by a finding nobody can clear."
        )


def test_the_two_deployments_agree() -> None:
    """They read one expression on purpose; a divergence means the planner and the worker disagree
    about what the estate owns, and only one of them gates the purge."""
    found = _platform_buckets(_render("--set", "catalog.multibase.dataBases[0]=s3://second-store/data"))

    assert len(set(map(tuple, found.values()))) == 1, f"the maintenance deployments carry different platform buckets: {found}"


def test_the_DEFAULT_render_gains_nothing() -> None:
    """`catalog.multibase.dataBases` is empty by default and the feature is off, so an estate that
    opted into nothing must see exactly the buckets it did before."""
    buckets = _platform_buckets(_render())

    for name, listed in buckets.items():
        assert "second-store" not in listed, f"{name} invented a bucket nobody declared: {listed}"


def test_an_empty_platform_set_adds_no_empty_bucket() -> None:
    """With observability off and `minio.buckets` empty the platform set is the empty string, which
    splits to one empty name. Only the declared base may remain."""
    docs = _render("--set", "observability.enabled=false", "--set", "minio.buckets=null", "--set", "catalog.multibase.dataBases[0]=s3://second-store/data")
    found = _maintenance_env(docs, "MAINTENANCE_S3_PLATFORM_BUCKETS")

    assert set(found) == set(_MAINTENANCE), f"the platform-bucket env is missing from: {sorted(set(_MAINTENANCE) - set(found))}"
    for name, value in found.items():
        assert value == "second-store", f"{name} carries an empty or stray platform bucket: {value!r}"
