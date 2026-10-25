"""A bucket the estate DECLARES must never reach `orphan_buckets`.

That category's contract is that a clean drift report certifies the estate's storage state, and the
trash purge gates on it. So a finding no operator can action holds the total above zero forever and
blocks reclamation with it — the maintenance config's own comment records `rask-observability` doing
exactly that until `objectStore.buckets` was wired into `MAINTENANCE_S3_PLATFORM_BUCKETS`.

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

from tests.unit.chart_render import PREREQ_ARGS, RAY_ARGS


_ROOT = Path(__file__).resolve().parents[2]
_BASE = [
    "--set",
    "image.localImages=true",
    *PREREQ_ARGS,
    *RAY_ARGS,
    "--set-string",
    "auth.oidc.issuer=http://dex.local:5556",
]
_MAINTENANCE = ("rask-maintenance",)


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


def test_a_renamed_root_is_exempt_under_its_new_name_only() -> None:
    """Maintenance exempts the bucket it sweeps, `MAINTENANCE_S3_BUCKET`, so a renamed root must reach that env, and the
    platform set must not carry the default spelling a second time: that would be a stray exemption, a bucket nothing
    makes, behind which a real orphan of that name would hide.
    """
    values = yaml.safe_load((_ROOT / "chart" / "values.yaml").read_text())
    docs = _render("--set", "objectStore.bucket=root-x", "--set", "objectStore.buckets={extra-platform}")
    found = _platform_buckets(docs)

    assert set(found) == set(_MAINTENANCE), f"the platform-bucket env is missing from: {sorted(set(_MAINTENANCE) - set(found))}"
    for name, buckets in found.items():
        assert buckets == ["extra-platform"], f"{name} exempts {buckets}, wanted only the declared platform bucket (not {values['objectStore']['bucket']!r})"
    root = _maintenance_env(docs, "MAINTENANCE_S3_BUCKET")
    assert root == dict.fromkeys(_MAINTENANCE, "root-x"), f"the renamed root is not the bucket maintenance sweeps and exempts: {root}"


def test_an_empty_platform_set_adds_no_empty_bucket() -> None:
    """With `objectStore.buckets` empty the platform set is the empty string, which splits to one empty name.
    Only the declared base may remain."""
    docs = _render("--set", "objectStore.buckets=null", "--set", "catalog.multibase.dataBases[0]=s3://second-store/data")
    found = _maintenance_env(docs, "MAINTENANCE_S3_PLATFORM_BUCKETS")

    assert set(found) == set(_MAINTENANCE), f"the platform-bucket env is missing from: {sorted(set(_MAINTENANCE) - set(found))}"
    for name, value in found.items():
        assert value == "second-store", f"{name} carries an empty or stray platform bucket: {value!r}"
