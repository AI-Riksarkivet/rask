"""The local estate's release lives in its own namespace, `rask`, and every tool that finds it looks there.

The chart has always been namespace-agnostic (`.Release.Namespace` throughout, no literal namespace), but
the deploy path named none, so helm installed into the kubeconfig's current namespace — `default` — beside
anything else the cluster ran there. Owner decision 2026-09-23: the release gets its own namespace. The
defect class this pins is a tool that still looks in the old place: `scripts/helm.sh` probing for the AGE
Postgres (the SQL release store) in `default` would decide no SQL store exists, and `scripts/k3s-pins.sh`
capturing `default` would report an estate with no workloads — both silently, both to a caller that then
acts on the answer.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

from tests.unit.k3s_recipe import K3S_UP_PREREQUISITES, REPO, captured_upgrade_call, make_value


def _fake(path: pathlib.Path, body: str) -> pathlib.Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_namespace_is_declared_once() -> None:
    assert make_value("$(NAMESPACE)") == "rask"


def test_k3s_up_installs_the_release_into_its_namespace(tmp_path: pathlib.Path) -> None:
    flags, namespace = captured_upgrade_call(tmp_path, "k3s-up", live=None, up_to_date=K3S_UP_PREREQUISITES)

    assert namespace == "rask", f"`make k3s-up` ran helm with HELM_NAMESPACE={namespace!r}; the release belongs in `rask`"
    assert "--create-namespace" in flags, "the first install must create the namespace it installs into"


def test_k3s_converge_upgrades_the_release_where_it_lives(tmp_path: pathlib.Path) -> None:
    _, namespace = captured_upgrade_call(tmp_path, "k3s-converge", live={"image": {"localImages": True}}, variables={"TAG": "t"})

    assert namespace == "rask", f"`make k3s-converge` looked for the release in {namespace!r}"


def test_the_helm_wrapper_probes_for_the_sql_store_in_the_releases_namespace(tmp_path: pathlib.Path) -> None:
    """The wrapper picks the release store by asking the AGE Postgres; asked in the wrong namespace it
    finds no pod and silently takes the Secret store."""
    record = tmp_path / "kubectl-args"
    kubectl = _fake(tmp_path / "kubectl", f'printf "%s\\n" "$*" >> "{record}"; exit 1\n')
    helm = _fake(tmp_path / "helm", "exit 0\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "kubectl").symlink_to(kubectl)
    (bindir / "helm").symlink_to(helm)

    done = subprocess.run(  # noqa: S603
        ["bash", str(REPO / "scripts" / "helm.sh"), "list"],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{bindir}:{os.environ['PATH']}", "HELM_NAMESPACE": "rask", "KUBECONFIG": "/dev/null", "HOME": str(tmp_path)},
    )

    assert done.returncode == 0, done.stderr
    probes = record.read_text(encoding="utf-8").splitlines()
    assert any("rask-age-0" in p for p in probes), f"the wrapper did not probe for the AGE pod: {probes}"
    assert all("-n rask" in p for p in probes if "rask-age-0" in p), f"the AGE pod was looked for outside the release namespace: {probes}"


def test_k3s_pins_captures_the_releases_namespace(tmp_path: pathlib.Path) -> None:
    record = tmp_path / "kubectl-args"
    kubectl = _fake(
        tmp_path / "kubectl",
        f"""printf "%s\\n" "$*" >> "{record}"
case "$*" in
  *"namespace kube-system"*) printf %s 11111111-aaaa-4bbb-8ccc-000000000001 ;;
  *raycluster*) exit 1 ;;
  *) echo '{{"items": []}}' ;;
esac
""",
    )

    done = subprocess.run(  # noqa: S603
        ["bash", str(REPO / "scripts" / "k3s-pins.sh"), str(tmp_path / "pins.yaml")],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        env={"PATH": os.environ["PATH"], "KUBECTL": str(kubectl), "KUBECONFIG": "/dev/null"},
    )

    assert done.returncode == 0, done.stderr
    workload_reads = [line for line in record.read_text(encoding="utf-8").splitlines() if "get deploy" in line or "raycluster" in line]
    assert workload_reads and all("-n rask" in line for line in workload_reads), f"k3s-pins read workloads outside `rask`: {workload_reads}"
