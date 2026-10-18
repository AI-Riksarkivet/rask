"""No store identity may be blank: the render refuses it.

Each governed service is handed the NAMES of its scoped S3 key pair (`LANCE_DAPR_SECRET_KEY` with
`LANCE_DAPR_SECRET_S3_FIELD` and `LANCE_DAPR_SECRET_S3_ID_FIELD`, `LINEAGE_…`, `MAINTENANCE_…`) and fetches both halves
from the Dapr secret store (ADR 0166), while the dev store's seed writes a plane's pair only when its identity is named.
The two can disagree only where an identity is blank, and the only other pair a plane could present is the store's root,
which on RustFS signs every STS session token (ADR 0168), so a blank identity is refused at render. That every pair a
service is told to read is the one the seed writes is read by running the seed
(`test_the_dev_openbao_is_seeded_by_its_own_pod.py`).

A sidecar logs in to the secret store with a token projected for one audience, which every app's role is bound to
([[XC-165]]): a role with no audience admits any token of its ServiceAccount, so a blank audience is refused too.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.chart_render import REPO, render_chart_text


#: Each identity the render refuses blank, by the value that names it: the planes' scoped pairs, each refused by the
#: template that tells a plane its field names, and the audience every sidecar's store login is bound to.
_IDENTITIES = ("objectStore.catalogAccessKey", "objectStore.lineageAccessKey", "objectStore.maintenanceAccessKey", "openbao.identity.audience")

_BASE = ("--set", "image.localImages=true")


@pytest.mark.parametrize("identity", _IDENTITIES)
def test_a_blank_store_identity_fails_the_render(identity: str) -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *_BASE, "--set", f"{identity}=")
    assert f"{identity} names" in refused.value.stderr, refused.value.stderr[-600:]
