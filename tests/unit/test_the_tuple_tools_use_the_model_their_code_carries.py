"""The estate's tuple tools check and write against the model their own code carries ([[LH-201]]).

A tuple request that names no `authorization_model_id`, or names the store's newest model, is validated against
whichever image wrote last. LH-201 lets a legacy image's narrower body stay the newest for as long as that image
boots (the 2026-09-03 compute, controlplane and flows images still provision their own), and against it every
`estate:rask` request fails on `type 'estate' not found`. So `make fga-estate-migrate` finds its store
and model by `write_model.carried_model`, the rule `bootstrap-admin` uses: the pinned store, else the newest named
`lance-catalog`, and in it the model whose body is the code's own `model.json`.

RUN, not read, against `openfga_stub`. The migration is piped to `python -` exactly as `make fga-estate-migrate`
pipes it into the catalog pod.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.openfga_stub import BY_NAME, CARRYING, PINNED, STORE, Recorded, openfga


_ROOT = Path(__file__).resolve().parents[2]
_MIGRATE = _ROOT / "scripts" / "fga_estate_root_migrate.py"

#: One principal on each carried rung of the old root; the migration grants each on `estate:rask`.
_OLD_ROOT = {("user:alice", "owner", "warehouse:lance_catalog"), ("user:service-lineage", "event_stager", "warehouse:lance_catalog")}

_STORES = pytest.mark.parametrize(("stores", "pin"), [(BY_NAME, ""), (PINNED, STORE)], ids=["by-name", "pinned"])


def _not_carrying(recorded: Recorded) -> str:
    """Every check and write that names another model than the carried one, as an assertion message."""
    wrong = [r for r in recorded.requests if r.get("authorization_model_id") != CARRYING]
    return f"{len(wrong)} of {len(recorded.requests)} tuple requests do not name the carried model {CARRYING}: {wrong[:1]}" if wrong else ""


@_STORES
def test_the_estate_migration_grants_against_the_model_it_carries(stores: list[dict[str, str]], pin: str) -> None:
    recorded = Recorded(written=set(_OLD_ROOT))
    with openfga(stores, recorded) as url:
        env = {"PATH": os.environ["PATH"], "RASK_FGA_API_URL": url, **({"RASK_FGA_STORE_ID": pin} if pin else {})}
        done = subprocess.run([sys.executable, "-"], input=_MIGRATE.read_text(), env=env, capture_output=True, text=True, timeout=60, check=False)

    assert recorded.requests, f"the migration sent no tuple request at all:\n{done.stdout}{done.stderr}"
    assert not _not_carrying(recorded), _not_carrying(recorded)
    assert done.returncode == 0, f"the migration exited {done.returncode}:\n{done.stdout}{done.stderr}"
    assert {(user, relation, "estate:rask") for user, relation, _ in _OLD_ROOT} <= recorded.written
