"""The seven web pods stop holding the estate's shared service bearer.

Q17-7 / §F2-3. `dapr_auth.service_principal` says the cost outright: "with one shared token across an
allowlist, any holder can pick the highest-privileged name on it". The privileged binding has since
closed that for five subjects — a privileged name presented with the shared token is refused — so the
warning is no longer true of the cascade. It is still true of everyone else on the allowlist, and the
widest holder is the frontend: SEVEN zone Deployments mount
`{{ .Release.Name }}-dapr-app-token`, and the chart's own note on the privileged block records what
that buys an attacker — "Reading one web pod's environment was the whole attack."

WHY THIS SUBJECT AND NOT THE OTHER THREE. `service-ingest`, `service-maintenance` and `notifications`
also still share it, and each needs a service change first: none of them reads a dedicated token
(measured 2026-09-07, 0 `dedicated_token` references in all three), and adding a subject to the
privileged list WITHOUT its client half breaks it immediately — the measured 2026-08-26 lesson the
chart records, where rendering the server-side expectation alone 401'd every stage runner call until it was
reverted. The BFF is different: it already reads `env.LINEAGE_SERVICE_TOKEN` and stamps
`x-lance-service-identity` (`bff.ts:195,369`), so CHANGING WHICH SECRET FILLS THAT ENV *is* the client
half. No frontend code moves.

It has no Dapr sidecar, so it cannot read the secret store — the same shape as the Ray head, and it
takes the same route: `secretKeyRef` onto infra-credentials, which external-secrets already syncs
from OpenBao.

THE PAIR MUST MOVE TOGETHER, as it did for the trainer: a subject named privileged whose pods still
mount the shared token is refused on every call, and a pod given a dedicated token for a subject
nobody made privileged authenticates against nothing. Both halves are asserted here, under the flag
and without it.
"""

from __future__ import annotations

import pathlib
import sys

import pytest


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


WEB = "service-web"
ON = "auth.dedicatedServiceCredentials=true"


def _web_token_refs(*set_values: str) -> dict[str, dict]:
    """`{deployment: secretKeyRef}` for every Deployment mounting `LINEAGE_SERVICE_TOKEN`."""
    found: dict[str, dict] = {}
    for doc in _rendered_docs(*set_values):
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env") or []:
                if env["name"] == "LINEAGE_SERVICE_TOKEN":
                    found[doc["metadata"]["name"]] = (env.get("valueFrom") or {}).get("secretKeyRef") or {}
    return found


def _lineage_privileged(*set_values: str) -> set[str]:
    for doc in _rendered_docs(*set_values):
        if doc.get("kind") != "Deployment" or "lineage" not in doc["metadata"]["name"]:
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env") or []:
                if env["name"] == "LINEAGE_PRIVILEGED_SUBJECTS":
                    return {s for s in str(env.get("value") or "").split(",") if s}
    return set()


def test_the_web_identity_is_privileged_at_the_lineage_door() -> None:
    """The server half. Without it the dedicated token below authenticates against nothing — the door
    would still accept the shared one for this subject, so the credential change buys no control."""
    assert WEB in _lineage_privileged(ON), "service-web is not privileged, so the shared token still works for it"


def test_no_web_pod_mounts_the_SHARED_bearer() -> None:
    """The defect: seven Deployments holding the token that authenticates every other service."""
    shared = {name: ref for name, ref in _web_token_refs(ON).items() if "web" in name and str(ref.get("name", "")).endswith("-dapr-app-token")}
    assert not shared, f"these web pods still mount the estate's shared service bearer: {sorted(shared)}"


def test_every_web_pod_takes_its_own_credential_off_infra_credentials() -> None:
    """It has no Dapr sidecar, so it takes the same route the Ray head does — and every zone must move,
    since one left behind keeps the shared token readable from a pod."""
    web = {name: ref for name, ref in _web_token_refs(ON).items() if "web" in name}
    assert web, "no web Deployment mounts LINEAGE_SERVICE_TOKEN at all"
    for name, ref in web.items():
        assert str(ref.get("name", "")).endswith("-infra-credentials"), f"{name}: {ref}"
        assert ref.get("key") == f"service-token-{WEB}", f"{name}: {ref}"


def test_infra_credentials_carries_the_key_the_web_pods_ask_for() -> None:
    """A `secretKeyRef` at a key nothing writes is a pod that cannot start."""
    secrets = [d for d in _rendered_docs(ON) if d.get("kind") == "Secret" and d["metadata"]["name"].endswith("-infra-credentials")]
    assert secrets, "no infra-credentials Secret rendered"
    assert f"service-token-{WEB}" in (secrets[0].get("stringData") or {})


def test_the_seeded_token_and_the_mounted_token_are_THE_SAME_STRING() -> None:
    """`secrets.compare_digest` at the door, so anything but an exact match is a 401 that renders as
    nothing. One helper feeds both, and this is what proves they did not drift."""
    docs = _rendered_docs(ON)
    secrets = [d for d in docs if d.get("kind") == "Secret" and d["metadata"]["name"].endswith("-infra-credentials")]
    mounted = (secrets[0].get("stringData") or {})[f"service-token-{WEB}"]

    seeds = [d for d in docs if d.get("kind") == "Job" and "openbao" in d["metadata"]["name"]]
    if not seeds:
        pytest.skip("no openbao seed Job on this profile — nothing to compare against")
    assert f"service-token-{WEB}={mounted}" in str(seeds[0]["spec"]["template"]["spec"]["containers"][0])


def test_with_the_flag_OFF_nothing_changes() -> None:
    """Opt-in, like every other half of this control: an estate that has not turned dedicated
    credentials on keeps the shared token and is not broken by this landing."""
    web = {name: ref for name, ref in _web_token_refs("auth.dedicatedServiceCredentials=false").items() if "web" in name}
    assert web, "no web Deployment mounts LINEAGE_SERVICE_TOKEN at all"
    for name, ref in web.items():
        assert str(ref.get("name", "")).endswith("-dapr-app-token"), f"{name} changed with the flag off: {ref}"
