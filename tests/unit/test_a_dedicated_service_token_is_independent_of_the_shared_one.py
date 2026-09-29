"""A privileged identity's dedicated credential is independent material, and the chart's two writers agree.

[[ZT-001]]. `service_kit.governed.dapr_auth` introduces the dedicated pair for one stated reason: a
subject NOT on the privileged allowlist authenticates with the estate's shared `APP_API_TOKEN`, so
without a per-identity credential "any holder of that one token may claim ANY allowlisted service" —
including identities holding a write and publish rung on every warehouse. A dedicated token computed
FROM that shared token leaves the property it exists to remove exactly where it was.

`lance.dedicatedServiceToken` has TWO writers that must produce the same bytes: `openbao.yaml` seeds
the store the door reads, and `infra-credentials.yaml` holds the copy a daprd-less consumer mounts.
`dapr_auth.service_principal` compares them with `secrets.compare_digest`, so a near-miss is a 401 that
nothing renders as an error — which is why cross-writer agreement is gated here and not just observed.

ONE BRANCH IS OUT OF REACH HERE AND IS NOT COVERED: the helper's `lookup` of the live Secret, and with
it the rule that a looked-up value still equal to the old derivation is discarded rather than carried
forward. `helm template` has no cluster, so `lookup` returns empty and every assertion below exercises
the generate path. Removing the discard passes this file. It is proven by deploying instead — an estate
whose Secret holds derivable values must come back holding independent ones.
"""

from __future__ import annotations

import base64
import pathlib
import re
import subprocess

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "chart"
#: Any value works — the property is that KNOWING it buys nothing, not that this one is weak.
APP_TOKEN = "a-real-operator-supplied-app-token"


def _render(*overrides: str) -> str:
    helm = ROOT / ".localbin/helm"
    exe = str(helm) if helm.exists() else "helm"
    argv = [
        exe,
        "template",
        str(CHART),
        "--set",
        "image.localImages=true",
        "--set-string",
        "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
        "--set-string",
        "frontend.oidc.publicIssuer=http://localhost:8080/dex",
        "--set-string",
        "frontend.oidc.publicOrigin=http://localhost:8080",
        "--set-string",
        f"dapr.appToken={APP_TOKEN}",
    ]
    for override in overrides:
        argv += ["--set-string", override]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def _mounted(rendered: str) -> dict[str, str]:
    """`service-token-<identity>` entries from every rendered Secret — the copy a daprd-less pod mounts."""
    found: dict[str, str] = {}
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") != "Secret":
            continue
        for key, value in (doc.get("data") or {}).items():
            if key.startswith("service-token-"):
                found[key.removeprefix("service-token-")] = base64.b64decode(value).decode()
        for key, value in (doc.get("stringData") or {}).items():
            if key.startswith("service-token-"):
                found[key.removeprefix("service-token-")] = value
    return found


def _seeded(rendered: str) -> dict[str, str]:
    """Each identity's credential from the OpenBao seed Job — the copy the DOOR reads.

    ONE SECRET PER IDENTITY since [[XC-072]] (`bao kv put secret/service-token-<id> token=<value>`),
    not a field of the shared `lance` bundle. Dapr grants by secret NAME and never by field, so the
    split is what makes a per-app scope able to say anything -- and this walk has to follow it or it
    silently compares nothing and both writers "agree".
    """
    return {identity: value.strip("'") for identity, value in re.findall(r"secret/service-token-(\S+) token=(\S+)", rendered)}


def test_the_two_writers_agree_byte_for_byte() -> None:
    """A door seeded with one value and a pod mounting another is a 401 with no error anywhere."""
    rendered = _render()
    mounted, seeded = _mounted(rendered), _seeded(rendered)
    shared = sorted(set(mounted) & set(seeded))
    assert shared, "the two writers name no identity in common — one of the two walks above is stale"

    disagreeing = [i for i in shared if mounted[i] != seeded[i]]
    assert not disagreeing, f"mounted and seeded halves differ for {disagreeing} — every call from those identities is a silent 401"


def _helper_body() -> str:
    """The whole `lance.dedicatedServiceToken` define, to the next define or end of file.

    Not a lazy regex to `{{- end -}}`: the body nests three of them, so a non-greedy match returns the
    first few lines and a gate built on it reports clean on everything below. Measured — it passed a
    mutation that reintroduced the app token five lines past the cut.
    """
    helper = (CHART / "templates/_helpers.tpl").read_text()
    start = helper.index('{{- define "lance.dedicatedServiceToken" -}}')
    nxt = helper.find("{{- define ", start + 1)
    return helper[start : nxt if nxt != -1 else len(helper)]


def test_the_shared_app_token_is_only_ever_compared_against_never_built_from() -> None:
    """The dependence gated structurally, because for a GENERATED credential it cannot be gated behaviourally.

    The behavioural form — render twice with different `dapr.appToken` and compare — proves nothing:
    `randAlphaNum` differs between renders whatever the helper reads, so it is satisfied by a dependence
    just as readily as by independence. What IS checkable is the shape: the helper recomputes the old
    derivation to RECOGNISE a compromised value, so the app token may appear in that comparison and
    nowhere else — never on the path that decides what the credential is.
    """
    body = _helper_body()
    reads = [line.strip() for line in body.splitlines() if ".Values.dapr." in line]
    assert len(reads) <= 1, f"the helper reads the shared token on {len(reads)} lines; only the guessable-value check may"
    for line in reads:
        assert "$guessable" in line, f"the shared token is read outside the guessable-value check: {line}"

    assigns = [line.strip() for line in body.splitlines() if "set $memo $identity" in line]
    assert assigns, "nothing assigns the memo — this walk is stale and every assertion above is unmoored"
    for line in assigns:
        assert "sha256sum" not in line, f"the credential is assigned from a digest of the shared token: {line}"
