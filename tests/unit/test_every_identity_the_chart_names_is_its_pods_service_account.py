"""Every identity the chart names for a service is the subject of the ServiceAccount that service's pod runs as.

[[LH-474]], ADR 0156 C5. A door admits any token the cluster's service-account issuer signs as
`kubernetes~system:serviceaccount:<ns>:<sa>`, and OpenFGA alone decides what it may do. So a grant the chart writes,
an author a service stamps on what it signs, and the identity it signs as must each be the subject of the account its
pod actually runs as, in the namespace the release is installed in. A name that differs authenticates nobody: the
grant is held by no caller, and a signed event authored as another subject is authorized against tuples its signer
does not hold, so lineage refuses it and nothing reports the loss.

Rendered into a namespace other than `default`, so a subject built from a hand-typed namespace cannot pass.
"""

from __future__ import annotations

import json

from tests.unit.chart_render import env_of, render


NAMESPACE = "lakehouse"
RENDERED = (
    "--namespace",
    NAMESPACE,
    "--set",
    "image.localImages=true",
    "--set",
    "auth.enabled=true",
    "--set",
    "maintenance.enabled=true",
    "--set",
    "maintenance.vendWriteCredentials=true",
)


def _subject(account: str) -> str:
    return f"kubernetes~system:serviceaccount:{NAMESPACE}:{account}"


def test_every_service_identity_is_the_subject_of_its_own_pods_service_account() -> None:
    docs = render(*RENDERED)
    accounts: dict[str, str] = {}
    named: list[tuple[str, str, str]] = []
    hook = ""
    for doc in docs:
        spec = (doc.get("spec") or {}).get("template", {}).get("spec") or {}
        name = doc.get("metadata", {}).get("name", "")
        if doc.get("kind") == "Deployment":
            accounts[name] = spec["serviceAccountName"]
            for container in spec["containers"]:
                env = env_of(container)
                named += [(name, key, env[key]) for key in ("RASK_SIGNING_IDENTITY", "MAINTENANCE_CATALOG_SERVICE_IDENTITY") if key in env]
        if doc.get("kind") == "Job" and name.endswith("-bootstrap-admin"):
            hook = spec["containers"][0]["args"][0]

    wrong = {f"{pod} {key}": value for pod, key, value in named if value != _subject(accounts[pod])}
    assert {key for _pod, key, _value in named} == {"RASK_SIGNING_IDENTITY", "MAINTENANCE_CATALOG_SERVICE_IDENTITY"}, f"the render names {named}"
    assert not wrong, f"identities that are not their pod's service-account subject: {wrong}"

    granted = {
        line.split(" = ", 1)[0].strip(): json.loads(line.split(" = ", 1)[1])
        for line in hook.splitlines()
        if line.strip().startswith(("maintainers =", "event_readers ="))
    }
    assert granted == {
        "maintainers": [_subject(accounts["rask-maintenance"])],
        "event_readers": [_subject(accounts["rask-notifications"])],
    }, f"the bootstrap hook grants {granted}"
