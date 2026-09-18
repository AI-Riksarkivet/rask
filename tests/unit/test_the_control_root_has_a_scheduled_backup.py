"""The control root has a SCHEDULED backup, and its credential arrives as a mounted file.

[[LH-110]]. The control root is the registry holding every project, warehouse, namespace binding and
bucket claim. Lose it and the Lance data survives while nothing can route to it — the one loss the
lakehouse cannot reconstruct from storage. `scripts/control_root_backup.py` shipped backup, verify,
restore and prune, and `docs/runbooks/RUNBOOK-restore.md` documented the drill; nothing ran it.

THE CREDENTIAL IS THE PART THE ROW DID NOT SAY. It offered three ways to ship the script and none of
them answered how the pod authenticates, and `main` called `s3_client()` bare — so all three land on
credentials in the CronJob's environment, the one delivery path this estate forbids. A CronJob pod
carries no Dapr sidecar, so ESO writing a Secret the pod MOUNTS is the path that fits it.

THE REFUSAL IS ASSERTED TOO. With the operator off, the job would render a pod referencing a Secret
nothing creates — `optional: false`, so it never starts, and a backup that never starts looks exactly
like one with nothing to do. The chart fails the render instead.
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


_ON = ("--set", "backups.controlRoot.enabled=true", "--set", "externalSecrets.enabled=true")


def _cronjob(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "CronJob" and "control-root-backup" in doc.get("metadata", {}).get("name", ""):
            return doc
    raise AssertionError("no control-root backup CronJob rendered")


def test_off_by_default_renders_nothing() -> None:
    """A backup writing into an unconfigured destination is worse than none, so it is opt-in."""
    assert not [d for d in _render() if "control-root-backup" in str(d.get("metadata", {}).get("name", ""))]


def test_enabled_renders_a_cronjob_that_runs_the_shipped_tool() -> None:
    container = _cronjob(_render(*_ON))["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]

    assert container["command"] == ["python", "/srv/control_root_backup.py"], container["command"]
    assert "backup" in container["args"], container["args"]


def test_the_credential_is_MOUNTED_and_never_an_env_row() -> None:
    """The whole point. An env row here would be a 31st rendered secret on the path the estate bans."""
    spec = _cronjob(_render(*_ON))["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    container = spec["containers"][0]
    mounts = {m["mountPath"] for m in container.get("volumeMounts", [])}

    assert any(v.get("secret") for v in spec.get("volumes", [])), f"no Secret volume — the credential has nowhere to come from but env: {spec.get('volumes')}"
    assert "--credentials-file" in container["args"], "the tool is invoked without --credentials-file, so it falls back to the environment chain"
    creds = container["args"][container["args"].index("--credentials-file") + 1]
    assert any(creds.startswith(m) for m in mounts), f"--credentials-file {creds!r} points outside every mount: {mounts}"
    for row in container.get("env", []):
        assert "valueFrom" not in row, f"{row['name']} arrives by secretKeyRef — the delivery path this estate forbids"


def test_the_tool_is_IN_the_image_it_is_invoked_from() -> None:
    """A CronJob naming a path no image carries fails at 03:00 with CreateContainerError, and the
    render that produced it is green. The final stage is what ships — the builder is discarded."""
    dockerfile = (REPO / ".docker" / "rest-catalog.dockerfile").read_text()
    final = dockerfile[dockerfile.rindex("FROM ") :]

    assert "control_root_backup.py" in final, "the tool is copied in the builder stage only, which hands over the venv and nothing else"


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


def test_enabling_the_backup_without_the_operator_is_REFUSED() -> None:
    """Rendering a pod that references a Secret nothing creates is a backup that never runs."""
    with pytest.raises(RuntimeError, match="externalSecrets"):
        _render("--set", "backups.controlRoot.enabled=true", "--set", "externalSecrets.enabled=false")
