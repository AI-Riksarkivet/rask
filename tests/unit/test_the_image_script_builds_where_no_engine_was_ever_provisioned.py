"""`scripts/dagger-image.sh` must build on a host that has never run `make dagger-engine`.

The script required the repo's named engine for BOTH delivery modes, and the reason it gave applied
to only one of them. --push genuinely needs that engine's CONFIG (Dagger speaks HTTPS and `publish`
has no --insecure flag, so the plain-HTTP dev registry is unreachable without it). --load needs no
config at all; what it needs is to not split the BuildKit CACHE across two engines. A cache can only
be split where a cache persists — and CI is a fresh runner with no engine, no state volume and no
`make dagger-engine` step. So the guard refused every CI build to protect something that was not
there: `e2e-stack` and `e2e-ray` both died on `!! dagger-engine-rask is not running` the first time
they reached this script, one step past the dagger-CLI fix that let them reach it.

THE SCRIPT ITSELF IS UNDER TEST, not a copy of its logic. `docker` and `dagger` are stubbed onto the
front of PATH — the real PATH, kept intact, because a scrubbed one is its own source of wrong answers
— and the stubs are what a runner would answer: no such container, no such volume, a build that
exports a tarball. The three cases are the three the guard now distinguishes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "dagger-image.sh"

_DAGGER_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
for a in "$@"; do case "$a" in --path=*) : > "${a#--path=}";; esac; done
exit 0
"""

#: `volume inspect` is the only answer that varies — it is what tells the guard whether a warm cache
#: exists to be split. Everything else models a runner: no engine container, a working daemon.
_DOCKER_STUB = """#!/usr/bin/env bash
case "$1 $2" in
  "inspect -f")   exit 1 ;;
  "volume inspect") exit ${STUB_VOLUME_EXISTS:-1} ;;
esac
case "$1" in
  load) echo "Loaded image ID: sha256:0000000000000000000000000000000000000000000000000000000000000000" ;;
  tag)  : ;;
  *)    exit 1 ;;
esac
"""


def _run(tmp_path: Path, *args: str, volume_exists: bool = False) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("dagger", _DAGGER_STUB), ("docker", _DOCKER_STUB)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_VOLUME_EXISTS": "0" if volume_exists else "1",
        "DAGGER_ENGINE_NAME": "absent-engine",
        "DAGGER_ENGINE_STATE": "absent-state",
    }
    env.pop("_EXPERIMENTAL_DAGGER_RUNNER_HOST", None)
    return subprocess.run(["bash", str(_SCRIPT), *args], cwd=_ROOT, env=env, capture_output=True, text=True, check=False)


def test_a_load_build_succeeds_with_no_engine_and_no_cache_volume(tmp_path: Path) -> None:
    """The CI shape. Nothing to split, so nothing to refuse."""
    done = _run(tmp_path, "--name", "gateway", "--tag", "gateway:test")

    assert done.returncode == 0, f"a --load build was refused on a host with no engine and no cache:\n{done.stderr}"
    calls = (tmp_path / "calls.log").read_text()
    assert "image --name=gateway" in calls, f"the build never reached dagger: {calls!r}"


def test_a_load_build_is_refused_when_the_cache_volume_is_RIGHT_THERE(tmp_path: Path) -> None:
    """The dev-host shape: `make dagger-engine` has run and the engine is merely stopped. Building
    now would provision a second engine and read cold past a warm cache, which is the whole reason
    the guard exists."""
    done = _run(tmp_path, "--name", "gateway", "--tag", "gateway:test", volume_exists=True)

    assert done.returncode != 0, "a stopped engine with its cache volume present was allowed to be bypassed"
    assert "absent-state" in done.stderr, f"the refusal does not name the volume it is protecting:\n{done.stderr}"


def test_a_push_is_refused_without_the_engine_whatever_the_cache_says(tmp_path: Path) -> None:
    """--push needs the CONFIG, not the cache, so no volume state can make it safe."""
    done = _run(tmp_path, "--name", "gateway", "--push", "127.0.0.1:5000/gateway:test")

    assert done.returncode != 0, "a --push to the plain-HTTP registry was allowed without the engine's insecure config"
    assert "insecure-registry" in done.stderr, f"the refusal does not give its reason:\n{done.stderr}"


@pytest.mark.parametrize("mode", ["load", "push"])
def test_an_operator_supplied_runner_host_still_wins(tmp_path: Path, mode: str) -> None:
    """The escape hatch the guard has always had: an explicitly exported engine is respected in both
    modes, and neither branch above may quietly take it away."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("dagger", _DAGGER_STUB), ("docker", _DOCKER_STUB)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    args = ["--name", "gateway", "--tag", "gateway:test"] if mode == "load" else ["--name", "gateway", "--push", "reg/gateway:t"]
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "_EXPERIMENTAL_DAGGER_RUNNER_HOST": "docker-container://chosen-by-the-operator",
    }
    done = subprocess.run(["bash", str(_SCRIPT), *args], cwd=_ROOT, env=env, capture_output=True, text=True, check=False)

    assert done.returncode == 0, f"an exported runner host was overridden by the guard ({mode}):\n{done.stderr}"
