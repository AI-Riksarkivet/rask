"""When two release stores both hold the release, the wrapper must use the one the owner named — and say so.

`scripts/helm.sh` exists because helm embeds the whole chart in every revision, and on 2026-08-15 the
release SECRET crossed Kubernetes' 1 MiB object limit so nothing could be stored; the release moved to
Postgres. Its header states the hazard: an invocation against the wrong backend "concludes the release
is absent and INSTALLS OVER A LIVE ESTATE... a silent success against the wrong backend is worse than
any error."

Its rule for picking a driver was *"a store with releases in it is the store in use"*, which is sound
only while exactly ONE store has them. MEASURED 2026-09-08 on this estate:

    SQL / Postgres   rask rev 42   Aug 28
    Secret store     rask rev 108  Sep 7

so the probe selected a history ten days stale, and an `upgrade` through the wrapper would have
rewritten the whole fleet from that manifest — the same harm the header describes, reached from the
other direction.

OWNER RULING 2026-09-08: the SECRET store is authoritative and the SQL rows are residue. So the
wrapper selects the Secret store and ANNOUNCES the divergence on every call.

The wrapper runs for real against a stub `kubectl` (it answers the AGE pod's IP) and a stub `helm` that
answers `list -aq` per driver and records the driver the final call was made with.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "helm.sh"

_KUBECTL = """#!/bin/sh
echo 10.0.0.9
"""

#: `list -aq` answers from the store the driver names; anything else records the driver it ran under.
_HELM = """#!/bin/sh
if [ "$1" = "list" ]; then
  if [ "${HELM_DRIVER:-}" = "sql" ]; then printf '%s' "$SQL_RELEASES"; else printf '%s' "$SECRET_RELEASES"; fi
  exit 0
fi
echo "driver=${HELM_DRIVER:-secret}" >> "$CALLS"
"""


def _run(tmp_path: Path, *, sql: str, secret: str) -> tuple[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("kubectl", _KUBECTL), ("helm", _HELM)):
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    calls = tmp_path / "calls"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "KUBECONFIG": str(tmp_path / "kubeconfig"),
        "PGPASSFILE": str(tmp_path / "pgpass"),
        "SQL_RELEASES": sql,
        "SECRET_RELEASES": secret,
        "CALLS": str(calls),
    }
    done = subprocess.run(["bash", str(WRAPPER), "upgrade", "rask", "chart"], env=env, capture_output=True, text=True, timeout=30, check=True)  # noqa: S603, S607
    return calls.read_text().strip(), done.stderr


@pytest.mark.parametrize(
    ("sql", "secret", "driver", "announced"),
    [
        pytest.param("rask\n", "rask\n", "driver=secret", True, id="both-stores-use-the-secret-store"),
        pytest.param("rask\n", "", "driver=sql", False, id="a-lone-sql-store-answers-from-sql"),
    ],
)
def test_the_wrapper_picks_the_store_the_ruling_names(tmp_path: Path, sql: str, secret: str, driver: str, announced: bool) -> None:
    called_with, stderr = _run(tmp_path, sql=sql, secret=secret)

    assert called_with == driver
    assert ("both stores hold a release" in stderr) is announced, stderr
