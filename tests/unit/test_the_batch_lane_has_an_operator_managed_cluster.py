"""The batch lane's Ray head is a RayCluster the operator owns, not a hand-applied Deployment.

[[CP-042]]. Measured 2026-09-18: all four Ray CRDs are installed, `kubectl get
rayservice,raycluster,rayjob -A` returns No resources found, `kuberay-operator` has run 52 days with
nothing to reconcile, and the head every stage and train job lands on is
`deploy/ray-lance-demo.yaml` — a plain Deployment whose own header says "NOT for production:
production is KubeRay (RayCluster CR)".

WHAT THAT COSTS IS NOT STYLE. Three chart seams select `ray.io/is-ray-node`, a label only KubeRay
applies: the Collector's `ray-pods` scrape job, the Ray NetworkPolicy peer, and by consequence four
of the five Ray alert rules. `kubectl get pods -A -l ray.io/is-ray-node=yes` → No resources found, so
all three watch an empty set and a Ray failure is invisible rather than paged.

`_ray-cluster-config.tpl` was factored for exactly this second consumer and said so in its header —
"A RayService wraps a cluster to run SERVE applications; a RayCluster is the cluster on its own, for
the batch lane that submits Jobs and serves nothing" — and that consumer was never written.

THE TWO MUST NOT BOTH RENDER. A RayService already wraps a cluster; rendering a RayCluster beside it
gives the estate two heads, two GCSes and two dashboards, and whichever one the medallion's address
does not name is the one nobody notices is idle.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER


REPO = Path(__file__).resolve().parents[2]


def _render(*extra: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    cmd += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    cmd += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    cmd += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    cmd += ["--set", "image.localImages=true", *extra]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(done.stderr)
    return [d for d in yaml.load_all(done.stdout, Loader=FAST_LOADER) if d]


def _kinds(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


def test_the_batch_cluster_is_off_by_default() -> None:
    """An estate pointed at an external Ray must not gain a second one it did not ask for."""
    assert not _kinds(_render(), "RayCluster")


def test_enabling_it_renders_a_RayCluster_the_operator_owns() -> None:
    cluster = _kinds(_render("--set", "ray.cluster.enabled=true"), "RayCluster")

    assert len(cluster) == 1, f"expected exactly one RayCluster, got {len(cluster)}"
    assert cluster[0]["apiVersion"] == "ray.io/v1", cluster[0]["apiVersion"]
    assert cluster[0]["spec"].get("headGroupSpec"), "a RayCluster with no headGroupSpec is not a cluster"


def test_it_shares_ONE_cluster_shape_with_the_RayService() -> None:
    """Two spellings of the head is how the OTel wiring, the token auth and the credential plumbing
    drift — and drift here is invisible, because whichever object an estate does not render is never
    checked. Compared on the rendered spec, not on the template text."""
    cluster = _kinds(_render("--set", "ray.cluster.enabled=true"), "RayCluster")[0]["spec"]
    service = _kinds(_render("--set", "singleTenant.enabled=true"), "RayService")[0]["spec"]["rayClusterConfig"]

    assert cluster["headGroupSpec"] == service["headGroupSpec"], "the batch cluster's head has drifted from the Serve cluster's"


def test_a_RayService_and_a_RayCluster_never_render_together() -> None:
    """Two heads, two GCSes, two dashboards — and whichever the medallion's address does not name is
    the one nobody notices is idle."""
    with pytest.raises(RuntimeError, match="singleTenant|RayService|one Ray"):
        _render("--set", "ray.cluster.enabled=true", "--set", "singleTenant.enabled=true")


def _head(docs: list[dict]) -> dict:
    return next(d for d in docs if d.get("kind") == "RayCluster")["spec"]["headGroupSpec"]


_GCS = ("--set", "ray.cluster.enabled=true", "--set", "ray.cluster.gcsFaultTolerance.enabled=true")


def test_gcs_fault_tolerance_is_off_by_default() -> None:
    """It is ALPHA. Ray documents the RocksDB GCS backend as alpha, Linux-only and single-writer, so
    it is a toggle an operator reaches for deliberately, never a default that arrives with an upgrade."""
    env = {e["name"] for e in _head(_render("--set", "ray.cluster.enabled=true"))["template"]["spec"]["containers"][0].get("env", [])}

    assert "RAY_gcs_storage" not in env, "alpha GCS persistence is on by default"


def test_enabling_it_persists_the_gcs_to_a_volume() -> None:
    """Without the PVC the backend writes to the pod's own filesystem, which is the thing that dies —
    so the toggle would read as durability and deliver none."""
    head = _head(_render(*_GCS))
    container = head["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container.get("env", [])}

    assert env.get("RAY_gcs_storage") == "rocksdb", f"expected the embedded RocksDB backend, got {env.get('RAY_gcs_storage')!r}"
    path = env.get("RAY_gcs_storage_path")
    assert path, "the backend has no path, so it writes wherever the image's cwd happens to be"
    mounts = {m["mountPath"] for m in container.get("volumeMounts", [])}
    assert any(path.startswith(m) for m in mounts), f"RAY_gcs_storage_path {path!r} is outside every mount: {mounts}"
    claims = [v for v in head["template"]["spec"].get("volumes", []) if v.get("persistentVolumeClaim")]
    assert claims, "the GCS store is on an emptyDir, which dies with the pod it exists to outlive"


def test_it_refuses_a_head_that_could_run_TWO_writers() -> None:
    """THE RESTRICTION THAT IS EASY TO MISS. The RocksDB backend is single-writer, and a head that can
    briefly run two replicas corrupts it. `replicas: 1` is NOT sufficient — a rolling replacement
    starts the new pod before the old one goes, which is exactly two writers on one volume. The claim
    must therefore be single-attach and the head must not surge."""
    head = _head(_render(*_GCS))

    assert head.get("replicas", 1) == 1, f"a multi-replica head cannot own a single-writer store: {head.get('replicas')}"
    docs = _render(*_GCS)
    named = {v["persistentVolumeClaim"]["claimName"] for v in head["template"]["spec"].get("volumes", []) if v.get("persistentVolumeClaim")}
    rendered = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "PersistentVolumeClaim"}

    missing = named - set(rendered)
    assert not missing, f"the head mounts claims nothing creates, so the pod never starts: {missing}"
    gcs = next(d for name, d in rendered.items() if name.endswith("-ray-gcs"))
    assert gcs["spec"]["accessModes"] == ["ReadWriteOnce"], (
        f"the GCS claim is {gcs['spec']['accessModes']} — a single-writer store on a multi-attach claim is the one way this backend corrupts"
    )
