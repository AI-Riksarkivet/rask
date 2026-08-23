"""A small, local Ray for the driver tests.

Three things bite otherwise, and all three look like test failures rather than environment ones:

* **Ray 2.57 wraps a `uv run` driver in a uv runtime env, and that HANGS.** When the driver is
  launched by `uv run`, Ray sets `py_executable="uv run … python"` plus a `working_dir` zip of the
  cwd — and Ray's default excludes strip `.venv` from that zip. Each worker unzips, `cd`s in, and
  re-runs `uv run` against a DIFFERENT, empty project env, so `default_worker.py` dies
  `ModuleNotFoundError: No module named 'ray'` and retries every 60s forever. It never crashes, so
  there is no failure to read — three attempts to run this suite were abandoned as "Ray won't start".
  `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` is the documented lever and this repo ALREADY pulls it in six
  places (`scripts/dev-micro.sh`, four Makefile recipes, `chart/templates/rayservice.yaml`); this
  fixture simply never did. Set at MODULE scope, not inside the fixture, so it is in the environment
  before anything imports ray.
* `ray.init()` AUTO-CONNECTS to any cluster it can find, and this host has several discoverable
  heads — including stale ones left by earlier runs. The versions differ ("cluster Ray 2.56.1 /
  Python 3.14.3, this process Ray 2.57.0 / Python 3.13.12"), so the test dialled a stranger and
  reported a version mismatch. `address="local"` is what forecloses that.
* A default init sizes itself to the machine. This node has 64 CPUs, and standing that up per
  session is slow enough to look like a hang.

The caller's RAY_ADDRESS is read BEFORE it is overwritten. It used to be clobbered on the line above
the two that read it, which quietly made both branches dead: `num_cpus` was permanently None (so the
docstring's "two CPUs" never happened — the observed raylet was CPU,64) and the "connect to a head
the caller started" path could never be taken.
"""

from __future__ import annotations

import os

import pytest


# BEFORE any `import ray` anywhere in the session — see the module docstring.
os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")


@pytest.fixture(scope="session", autouse=True)
def _local_ray():
    ray = pytest.importorskip("ray")
    # READ FIRST. Overwriting RAY_ADDRESS before this is what made both branches below unreachable.
    caller_address = os.environ.get("RAY_ADDRESS")
    os.environ["RAY_ADDRESS"] = caller_address or "local"
    if not ray.is_initialized():
        # Connect to a head the caller started, when RAY_ADDRESS names one — starting a cluster per
        # session took longer than the test timeout twice on this host, and `local_mode` (the inline
        # scheduler that would have avoided it) was removed in Ray 2.57.
        ray.init(
            address=caller_address or "local",
            num_cpus=None if caller_address else 2,
            include_dashboard=False,
            configure_logging=False,
            ignore_reinit_error=True,
            log_to_driver=False,
        )
    yield
    if ray.is_initialized():
        ray.shutdown()
