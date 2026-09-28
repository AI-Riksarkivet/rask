"""bootstrap-admin checks and writes its tuples against the model its own image carries ([[LH-201]]).

A tuple request that names no `authorization_model_id` is validated against the store's NEWEST model, and
the newest is whichever image wrote last. Reproduced in the LH-201 review against OpenFGA v1.18.3: once an
image that still provisions its own narrower model (no `estate` type) is the newest, every `estate:rask`
write fails with `type 'estate' not found`, and this post-upgrade hook fails the release with it.

RUN, not read: the Job's own command is taken from the rendered chart and executed against a stub OpenFGA
that validates each tuple against the model the request names, the way OpenFGA does, so the assertions are
on the requests the hook really sends and on whether it really finishes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, containers, env_of, render
from tests.unit.openfga_stub import BY_NAME, CARRYING, PINNED, STORE, Recorded, openfga


def _run(job: dict[str, Any], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The container's own command, with this interpreter standing in for the image's `python`."""
    try:
        return subprocess.run([sys.executable, *job["command"][1:], *job["args"]], env=env, capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as exc:
        waited = exc.stdout
    pytest.fail(f"the hook was still waiting after 60 s, so it never found {STORE} holding {CARRYING}:\n{waited!s}")


@pytest.mark.parametrize(("stores", "pin"), [(BY_NAME, {}), (PINNED, {"RASK_FGA_STORE_ID": STORE})], ids=["by-name", "pinned"])
def test_every_tuple_request_names_the_model_the_image_carries(stores: list[dict[str, str]], pin: dict[str, str]) -> None:
    job = next(c for workload, _, c in containers(render(*DEFAULT_ARGS)) if workload.endswith("-bootstrap-admin"))
    recorded = Recorded()
    with openfga(stores, recorded) as url:
        done = _run(job, {"PATH": os.environ["PATH"], **env_of(job), **pin, "FGA_API_URL": url})

    unpinned = [r for r in recorded.requests if r.get("authorization_model_id") != CARRYING]
    assert recorded.requests, f"the hook sent no tuple request at all:\n{done.stdout}{done.stderr}"
    assert not unpinned, (
        f"{len(unpinned)} of {len(recorded.requests)} tuple requests do not name the model this image carries "
        f"({CARRYING}), so OpenFGA validates them against whichever image wrote last: {unpinned[0]}"
    )
    assert done.returncode == 0, f"the hook exited {done.returncode}:\n{done.stdout}{done.stderr}"
    assert any(obj == "estate:rask" for _, _, obj in recorded.written), "no estate grant reached the store"
