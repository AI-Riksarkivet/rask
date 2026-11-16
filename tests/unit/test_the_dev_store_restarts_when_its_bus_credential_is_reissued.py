"""The development store restarts itself when its bus credential is reissued.

RustFS reads its NATS credential file once, at start. Measured on the dev estate 2026-11-16 (RustFS 1.0.3, helm rev 28):
after the dev OpenBao reissued the `object-store` user and the bus revoked the old JWT, RustFS kept presenting the old
one (`authorization violation` every 4 s) for as long as it ran, with the new file already mounted, and published no
store event. So the container records its credential's digest when it starts, and its liveness probe fails once the
mounted file no longer has that digest, which has the kubelet restart it onto the new credential. Only the digest is
written outside the Secret's volume. The rendered commands run here against a stand-in for the store's health door.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from tests.unit.chart_render import render_fixture


def _store() -> dict:
    [workload] = [doc for doc in render_fixture("rustfs") if doc and doc.get("kind") == "StatefulSet"]
    [container] = workload["spec"]["template"]["spec"]["containers"]
    return container


def _run(command: list[str], root: Path) -> int:
    """Run a rendered command with the pod's absolute paths re-rooted under ``root`` and `curl` answering healthy."""
    curl = root / "bin" / "curl"
    curl.parent.mkdir(exist_ok=True)
    curl.write_text("#!/bin/sh\nexit 0\n")
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    rooted = [
        part.replace("/tmp/bus-credential-at-start", str(root / "tmp" / "at-start")).replace("/run/secrets/rustfs-bus", str(root / "bus")) for part in command
    ]
    return subprocess.run(rooted, env={"PATH": f"{curl.parent}:{os.environ['PATH']}"}, check=False).returncode  # noqa: S603


def test_the_store_is_restarted_once_its_mounted_bus_credential_is_another(tmp_path: Path) -> None:
    container = _store()
    started = container["lifecycle"]["postStart"]["exec"]["command"]
    alive = container["livenessProbe"]["exec"]["command"]
    (tmp_path / "bus").mkdir()
    (tmp_path / "tmp").mkdir()
    credential = tmp_path / "bus" / "nats.creds"
    credential.write_text("the credential the store started with")

    assert _run(started, tmp_path) == 0
    same = _run(alive, tmp_path)
    credential.write_text("the credential reissued while it ran")
    reissued = _run(alive, tmp_path)

    assert (same, reissued != 0) == (0, True)
    assert "the credential" not in "".join(path.read_text() for path in (tmp_path / "tmp").iterdir())
