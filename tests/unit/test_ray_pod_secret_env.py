"""The Ray pods hold the job secrets; the submission body holds none.

The render half of docs/DECISIONS.md "The Python estate audit"'s Jobs-API-echo P0 (the code half:
`services/medallion/tests/test_ray_submission_carries_no_secret.py`). Ray merges
`runtime_env.env_vars` OVER the pod's process env, so moving `S3_SECRET` and
`LINEAGE_SERVICE_TOKEN` to the pod keeps the job's `os.environ` contract byte-identical — but only
if the chart actually puts them there, which is what this gates.

`secretKeyRef` ONTO THE SECRETS THE ESTATE ALREADY OWNS, not new material: the S3 secret is
`<fullname>-infra-credentials/rustfs-secret-key` (the same object ExternalSecrets syncs from OpenBao
on the prod path) and the token is the Dapr app-token Secret's `token` key. A literal `value:` here
would put the credential in `helm get manifest` for anyone with read on the release — the exact
class of leak the infra-credentials header documents for the Dex secret.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


def _head_env() -> dict[str, dict]:
    docs = _rendered_docs("singleTenant.enabled=true")
    services = [d for d in docs if d.get("kind") == "RayService"]
    assert services, "no RayService rendered under singleTenant.enabled=true"
    containers = services[0]["spec"]["rayClusterConfig"]["headGroupSpec"]["template"]["spec"]["containers"]
    return {e["name"]: e for c in containers for e in (c.get("env") or [])}


@pytest.mark.parametrize("name", ["S3_SECRET", "LINEAGE_SERVICE_TOKEN"])
def test_the_head_holds_the_job_secret_from_a_secretKeyRef(name: str) -> None:
    env = _head_env()
    assert name in env, f"the Ray head carries no `{name}` — with the submission body no longer shipping it, a job's `os.environ[{name!r}]` has nothing to read"
    entry = env[name]
    assert "value" not in entry, f"`{name}` is a LITERAL in the rendered manifest — readable by anyone with read on the release"
    ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
    assert ref.get("name") and ref.get("key"), f"`{name}` is not a secretKeyRef: {entry}"
