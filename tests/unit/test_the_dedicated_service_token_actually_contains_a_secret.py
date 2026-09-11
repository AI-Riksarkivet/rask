"""A credential that authenticates a privileged identity must depend on a SECRET.

`lance.dedicatedServiceToken` derives every privileged service's credential as

    printf "%s-%s" <identity> .Values.dapr.appApiToken | sha256sum | trunc 40

and **`dapr.appApiToken` is defined nowhere**. The chart's value is `dapr.appToken` (`values.yaml`),
which is what `dapr-app-token.yaml` and `prod-credentials.yaml` both read; the helper is the only place
in the repository that names `appApiToken`. Go's `printf "%s"` does not render an undefined value as
the empty string — it renders the literal `%!s(<nil>)` — so the hashed string is

    "<identity>-%!s(<nil>)"

a pure function of a PUBLIC identity name. Measured 2026-09-11, and matched three ways so the claim
rests on no inference: two `helm template` runs whose `dapr.appToken` differs render the trainer's
token IDENTICALLY; the live cluster's `rask-infra-credentials` holds that same rendered value; and
`sha256("service-trainer-%!s(<nil>)")[:40]` reproduces it exactly. The value itself is deliberately not
written down here — it is a live credential until this deploys, and one a reader can recompute anyway,
which is the whole finding.

WHAT IT COSTS. These tokens are the credential the door compares with `secrets.compare_digest` to decide
whether a caller may CLAIM a privileged identity (`service_kit.governed.dapr_auth.service_principal`),
and they are issued to precisely the pods that hold no Dapr sidecar and so cannot read the secret store
— the trainer, the web BFF, the Ray stage lanes. Anyone who can read the chart can compute all of them,
and rotating `dapr.appToken` does not change one of them.

THE GATE IS THE DEPENDENCE, NOT THE SPELLING. Asserting `appToken` appears in the helper would pass the
day someone introduces a third name; asserting that the rendered token CHANGES when the secret changes
cannot be satisfied by any derivation that ignores it.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
_HELPER = REPO / "chart/templates/_helpers.tpl"
_VALUES = REPO / "chart/values.yaml"

#: Enough of a render to reach the Secret; the chart refuses half-governed combinations outright.
_RENDER = ("--set", "image.localImages=true", "--set", "auth.enabled=false", "--set", "frontend.oidc.enabled=false")
_TOKEN = re.compile(r'service-token-service-trainer:\s*"?([a-f0-9]{40})"?')
#: The seeder writes the same credential as a shell argument rather than as YAML, so it needs its own
#: shape. Both spellings are anchored on the 40-hex digest, never on the surrounding punctuation.
_SEEDED = re.compile(r"service-token-service-trainer=([a-f0-9]{40})")


def _render(*extra: str, template: str = "templates/infra-credentials.yaml") -> str:
    result = subprocess.run(  # noqa: S603 — helm from PATH, fixed arguments
        ["helm", "template", "rask", str(REPO / "chart"), *_RENDER, *extra, "--show-only", template],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"helm could not render the chart here: {result.stderr.strip()[:200]}")
    return result.stdout


def test_the_helper_reads_a_value_the_chart_defines() -> None:
    """The cheap half: a name no values file defines contributes nothing but a nil artifact."""
    helper = _HELPER.read_text(encoding="utf-8")
    values = _VALUES.read_text(encoding="utf-8")

    named = set(re.findall(r"\.Values\.dapr\.(\w+)", helper))
    for name in named:
        assert re.search(rf"^\s*{name}:", values, re.MULTILINE), (
            f"`lance.dedicatedServiceToken` reads `.Values.dapr.{name}`, which no values file defines — "
            f"Go renders it as the literal `%!s(<nil>)`, so the derived credential carries no secret"
        )


def test_rotating_the_app_token_changes_the_derived_credential() -> None:
    """THE GATE, and it is about DEPENDENCE rather than spelling.

    A derivation that ignores the secret cannot satisfy this, whatever it happens to name.
    """
    baseline = _TOKEN.search(_render())
    rotated = _TOKEN.search(_render("--set", "dapr.appToken=A-COMPLETELY-DIFFERENT-SECRET"))

    assert baseline and rotated, "the trainer's service token did not render — this gate would pass vacuously"
    assert baseline.group(1) != rotated.group(1), (
        "the derived service token is UNCHANGED by rotating dapr.appToken, so it contains no secret: it is "
        "sha256('<identity>-%!s(<nil>)') and anyone who reads the chart can compute every privileged "
        "identity's credential"
    )


def test_the_token_is_not_a_hash_of_public_strings_alone() -> None:
    """The same property stated so a reader can check it by hand, without helm.

    Named with the exact inputs because the defect was found by reproducing the rendered value and
    failing — the nil artifact, not the empty string, is what the template actually interpolates.
    """
    rendered = _TOKEN.search(_render())
    assert rendered, "nothing rendered — this gate would pass vacuously"

    for guessable in (b"service-trainer-%!s(<nil>)", b"service-trainer-", b"service-trainer"):
        assert rendered.group(1) != hashlib.sha256(guessable).hexdigest()[:40], (
            f"the credential equals sha256({guessable!r})[:40] — a public identity name and nothing else"
        )


def test_the_seeder_and_the_secret_derive_the_same_credential() -> None:
    """TWO PRODUCERS, ONE VALUE, and a divergence would 401 the whole estate at once.

    The seeder (`openbao.yaml`) writes the token into OpenBao, which is what a service with a Dapr
    sidecar reads to decide whether a caller may claim a privileged identity; the Secret
    (`infra-credentials.yaml`, or ESO syncing the same keys) is what the sidecar-less pods PRESENT.
    The two agree today only because both call `lance.dedicatedServiceToken` — the helper's own header
    says a second shape for the same job is how one of them ends up drifting, and this makes that
    advice enforceable. A drift is invisible in either render alone and costs every privileged call.
    """
    presented = _TOKEN.search(_render())
    expected = _SEEDED.search(_render(template="templates/openbao.yaml"))

    assert presented, "the Secret rendered no trainer token — this gate would pass vacuously"
    assert expected, "the seeder rendered no trainer token — this gate would pass vacuously"
    assert presented.group(1) == expected.group(1), (
        "the credential a pod PRESENTS and the credential the store EXPECTS are derived differently, so "
        "every privileged service call fails closed the moment both are deployed"
    )
