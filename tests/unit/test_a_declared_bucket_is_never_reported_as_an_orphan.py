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
    done = subprocess.run(["helm", "template", "rask", "chart/", *_BASE, *extra], cwd=_ROOT, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        pytest.skip(f"helm could not render the chart here: {done.stderr.strip()[:200]}")
    return [doc for doc in yaml.safe_load_all(done.stdout) if isinstance(doc, dict)]


def _platform_buckets(docs: list[dict]) -> dict[str, list[str]]:
    """`MAINTENANCE_S3_PLATFORM_BUCKETS`, split, per maintenance Deployment."""
    out: dict[str, list[str]] = {}
    for doc in docs:
        name = doc.get("metadata", {}).get("name")
        if doc.get("kind") != "Deployment" or name not in _MAINTENANCE:
            continue
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            for env in container.get("env") or []:
                if env["name"] == "MAINTENANCE_S3_PLATFORM_BUCKETS":
                    out[name] = [b for b in (env.get("value") or "").split(",") if b]
    return out


def test_both_maintenance_deployments_declare_the_set() -> None:
    """Anti-vacuity: the assertions below compare per-deployment, and a missing deployment would make
    them pass on a render that told one half of the service nothing."""
    found = _platform_buckets(_render())

    assert set(found) == set(_MAINTENANCE), f"the platform-bucket env is missing from: {sorted(set(_MAINTENANCE) - set(found))}"
    for name, buckets in found.items():
        assert "lance-catalog" in buckets, f"{name} does not even name the lakehouse bucket: {buckets}"


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
