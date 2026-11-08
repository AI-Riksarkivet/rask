"""No store identity may be blank: the render refuses it.

Every lakehouse workload's store identity is the token its pod projects for `objectStore.identity.audience`, which the
store trusts as its identity provider's client, exchanged for the role `objectStore.identity.roleArns.<component>` names
(ADR 0170, [[XC-084]]). A blank audience projects a token the store refuses, and a blank role is refused by STS at the
first exchange, so either is refused at render rather than at a pod's boot.

A sidecar logs in to the secret store with a token projected for one audience, which every app's role is bound to
([[XC-165]]): a role with no audience admits any token of its ServiceAccount, so a blank audience is refused too.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.chart_render import REPO, render_chart_text


#: Each identity the render refuses blank, by the value that names it: the store audience every workload's token is
#: projected for, a workload's role, and the audience every sidecar's secret-store login is bound to.
_IDENTITIES = ("objectStore.identity.audience", "objectStore.identity.roleArns.maintenance", "openbao.identity.audience")

_BASE = ("--set", "image.localImages=true")


@pytest.mark.parametrize("identity", _IDENTITIES)
def test_a_blank_store_identity_fails_the_render(identity: str) -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *_BASE, "--set", f"{identity}=")
    assert f"{identity} names" in refused.value.stderr, refused.value.stderr[-600:]
