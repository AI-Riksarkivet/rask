"""Every privileged identity's "dedicated" credential is computable from the shared app token.

[[ZT-001]]. `service_kit.governed.dapr_auth` introduces the dedicated pair for one stated reason: a
subject NOT on the privileged allowlist authenticates with the estate's shared `APP_API_TOKEN`, so
without a per-identity credential "any holder of that one token may claim ANY allowlisted service" —
including identities holding a write and publish rung on every warehouse.

`lance.dedicatedServiceToken` computes `sha256("<identity>-<dapr.appToken>")[:40]`. The identity half is
public — it is a name in `values.yaml`. So the whole set is a pure function of the shared token, which 13
pods carry, and the control is circular: holding the thing the dedicated token was introduced to
neutralise still yields every dedicated token.

**THIS TEST DOES NOT REFUSE THE RENDER, and that is deliberate rather than timid.** Closing the hole is a
deployment policy choice with real consequences — either the prod values enable ESO so an operator's own
material reaches OpenBao, or each token must be supplied and the render fails without it. Both make prod
undeployable in a way it is not today, and the estate's rule is that such a change is the owner's. What a
test CAN do without pre-empting that is make the property visible, reproducible, and unable to worsen
quietly: it derives a token exactly as the chart does and shows the rendered Secret contains it.

Delete this file when the derivation goes. It has no value once a dedicated token is independent material.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "chart"
#: Any value works — the point is that KNOWING it is sufficient, not that this particular one is weak.
APP_TOKEN = "a-real-operator-supplied-app-token"


def _derive(identity: str, app_token: str) -> str:
    """The chart's own expression, restated here so the test fails if the chart changes its shape.

    `printf "%s-%s" $identity $secret | sha256sum | trunc 40` — helm's `sha256sum` is hex, and `trunc 40`
    takes the first 40 characters of that hex string, not 40 bytes.
    """
    return hashlib.sha256(f"{identity}-{app_token}".encode()).hexdigest()[:40]


def _render() -> str:
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
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def _service_tokens(rendered: str) -> dict[str, str]:
    """`service-token-<identity>` entries from every rendered Secret, decoded."""
    import base64

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


def test_the_chart_still_renders_dedicated_tokens() -> None:
    """Without this the assertion below would pass by finding nothing to check."""
    tokens = _service_tokens(_render())
    assert tokens, "no `service-token-*` entries rendered — either the control was removed or this walk is stale"


def test_every_dedicated_token_is_computable_from_the_shared_app_token() -> None:
    """THE PROPERTY, stated so it cannot worsen quietly and so the owner's decision has a measurement.

    If this fails because a token is NO LONGER derivable: that is the fix landing, and this file should be
    deleted in the same commit rather than inverted — a test asserting independence belongs with the
    mechanism that provides it.
    """
    tokens = _service_tokens(_render())
    derivable = {i: t for i, t in tokens.items() if t == _derive(i, APP_TOKEN)}

    assert derivable == tokens, (
        "some dedicated tokens are no longer a pure function of `dapr.appToken` — the hole is closing and "
        f"this file should go with it. Still derivable: {sorted(derivable)}; independent: {sorted(set(tokens) - set(derivable))}"
    )
    assert len(derivable) >= 3, f"only {len(derivable)} privileged identities rendered; the blast radius measured here is the whole set"


def test_the_helper_has_not_changed_shape_without_this_test_noticing() -> None:
    """`_derive` restates the chart's expression, and a restatement drifts.

    Pinned against the template text so a change to the derivation reds HERE — where the consequence is
    written down — rather than silently making this file assert something the chart no longer does.
    """
    helper = (CHART / "templates/_helpers.tpl").read_text()
    body = re.search(r'define "lance\.dedicatedServiceToken".*?{{- end -}}', helper, re.DOTALL)
    assert body, "`lance.dedicatedServiceToken` is gone — if the derivation was replaced, delete this file"
    assert 'printf "%s-%s" $identity $secret | sha256sum | trunc 40' in body.group(0), (
        "the derivation changed shape; `_derive` above no longer restates it and every assertion here is now unmoored"
    )
