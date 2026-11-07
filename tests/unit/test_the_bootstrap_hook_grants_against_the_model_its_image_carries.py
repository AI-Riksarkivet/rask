"""bootstrap-admin checks and writes its tuples against the model its own image carries ([[LH-201]]).

A tuple request that names no `authorization_model_id` is validated against the store's NEWEST model, and
the newest is whichever image wrote last. Reproduced in the LH-201 review against OpenFGA v1.18.3: once an
image that still provisions its own narrower model (no `estate` type) is the newest, every `estate:rask`
write fails with `type 'estate' not found`, and this post-upgrade hook fails the release with it.

RUN, not read: the Job's own command is taken from the rendered chart and executed against a stub OpenFGA
that validates each tuple against the model the request names, the way OpenFGA does, so the assertions are
on the requests the hook really sends and on whether it really finishes. The stub admits only the bearer the
Job's projected token file holds ([[XC-077]]); the file sits where the rendered `RASK_FGA_TOKEN_FILE` says,
moved under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from lakehouse.governed.fga import user_object
from tests.unit.chart_render import DEFAULT_ARGS, containers, env_of, render
from tests.unit.openfga_stub import BY_NAME, CARRYING, PINNED, STORE, Recorded, openfga


#: The warehouse the hook grants the maintenance rung on: the default warehouse the chart names.
WAREHOUSE = "warehouse:lance_catalog"


def _run(job: dict[str, Any], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The container's own command, with this interpreter standing in for the image's `python`."""
    try:
        return subprocess.run([sys.executable, *job["command"][1:], *job["args"]], env=env, capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as exc:
        waited = exc.stdout
    pytest.fail(f"the hook was still waiting after 60 s, so it never found {STORE} holding {CARRYING}:\n{waited!s}")


@pytest.mark.parametrize(("stores", "pin"), [(BY_NAME, {}), (PINNED, {"RASK_FGA_STORE_ID": STORE})], ids=["by-name", "pinned"])
def test_every_tuple_request_names_the_model_the_image_carries(stores: list[dict[str, str]], pin: dict[str, str], tmp_path: Path) -> None:
    job = next(c for workload, _, c in containers(render(*DEFAULT_ARGS)) if workload.endswith("-bootstrap-admin"))
    token = tmp_path / env_of(job)["RASK_FGA_TOKEN_FILE"].lstrip("/")
    token.parent.mkdir(parents=True)
    token.write_text("projected-sa-jobs-token\n")
    recorded = Recorded()
    with openfga(stores, recorded, token_file=token) as url:
        done = _run(job, {"PATH": os.environ["PATH"], **env_of(job), **pin, "FGA_API_URL": url, "RASK_FGA_TOKEN_FILE": str(token)})

    unpinned = [r for r in recorded.requests if r.get("authorization_model_id") != CARRYING]
    assert recorded.refused == 0, f"OpenFGA refused {recorded.refused} of the hook's requests for want of its token:\n{done.stdout}{done.stderr}"
    assert recorded.requests, f"the hook sent no tuple request at all:\n{done.stdout}{done.stderr}"
    assert not unpinned, (
        f"{len(unpinned)} of {len(recorded.requests)} tuple requests do not name the model this image carries "
        f"({CARRYING}), so OpenFGA validates them against whichever image wrote last: {unpinned[0]}"
    )
    assert done.returncode == 0, f"the hook exited {done.returncode}:\n{done.stdout}{done.stderr}"
    assert any(obj == "estate:rask" for _, _, obj in recorded.written), "no estate grant reached the store"


def test_an_upgrade_revokes_only_what_the_releases_own_accounts_no_longer_declare(openfga_store: Any, tmp_path: Path) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """An upgrade converges the machine roles the release's own accounts hold on every object the hook grants on, and keeps
    every grant a door made ([[LH-563]], [[XC-164]]).

    Run against the pinned OpenFGA: before the hook, the store holds machine roles the release's own accounts were never
    declared, on the estate root and on the default warehouse (where an earlier release had declared lineage a maintainer),
    a client's machine roles granted at the doors (its ServiceAccount named with the release's prefix, which a client's own
    release may give it), and a person's `admin`. After it, the release's own accounts hold only what the render declares,
    and the client's grants and the person's rung stand.
    """
    job = next(c for workload, _, c in containers(render(*DEFAULT_ARGS)) if workload.endswith("-bootstrap-admin"))
    token = tmp_path / env_of(job)["RASK_FGA_TOKEN_FILE"].lstrip("/")
    token.parent.mkdir(parents=True)
    token.write_text("projected-sa-jobs-token\n")
    maintenance = user_object("kubernetes~system:serviceaccount:default:rask-sa-maintenance")
    lineage = user_object("kubernetes~system:serviceaccount:default:rask-sa-lineage")
    client = user_object("kubernetes~system:serviceaccount:default:rask-sa-a-tenant-service")
    door_made = {(client, role, "estate:rask") for role in ("decider", "member_expander", "event_reader")} | {(client, "maintenance", WAREHOUSE)}
    person = user_object("dex~alice")
    openfga_store.write(
        (maintenance, "decider", "estate:rask"),
        (lineage, "maintenance", WAREHOUSE),
        (lineage, "decider", WAREHOUSE),
        *door_made,
        (person, "admin", "estate:rask"),
    )

    done = _run(
        job,
        {
            "PATH": os.environ["PATH"],
            **env_of(job),
            "FGA_API_URL": openfga_store.url,
            "RASK_FGA_STORE_ID": openfga_store.store_id,
            "RASK_FGA_TOKEN_FILE": str(token),
        },
    )

    declared = {(user_object(account), role, "estate:rask") for account, roles in json.loads(env_of(job)["FGA_ESTATE_ROLES"]).items() for role in roles}
    declared.add((maintenance, "maintenance", WAREHOUSE))
    assert done.returncode == 0, f"the hook exited {done.returncode}:\n{done.stdout}{done.stderr}"
    assert openfga_store.tuples() == declared | door_made | {(person, "admin", "estate:rask")}
