"""A pod whose sidecar is handed an app-token Secret carries that Secret's checksum.

`dapr.io/app-token-secret` tells the injector which Secret holds the token daprd will present. The
injector reads it at POD CREATION, so a rotated Secret leaves the sidecar holding a dead credential —
the same staleness a `secretKeyRef` env has, one layer over. `checksum/dapr-app-token` is what turns a
rotation into a new pod template and therefore a restart; without it, nothing makes the pod notice.

THE RULE IS DERIVED FROM THE ANNOTATION, not from a list of services, because the two ways to lose this
look nothing alike and a name list would have caught neither. One was a DUPLICATE `annotations:` key
silently overwriting the checksum on four lakehouse Deployments — valid YAML, last key wins. The other
is a plain omission on two Deployments that never had it. Asking "is this pod given a token, and does
it carry the matching checksum" covers both, and covers a service nobody has written yet.

MEASURED 2026-09-23: 14 pods are given an app-token Secret; 12 carry the checksum.
"""

from __future__ import annotations

import yaml
from test_invariants import _helm_template


TOKEN_ANNOTATION = "dapr.io/app-token-secret"
CHECKSUM_ANNOTATION = "checksum/dapr-app-token"


def _pods_given_a_token() -> list[tuple[str, dict[str, str]]]:
    """(`Kind/name`, pod annotations) for every workload whose sidecar is handed the app token."""
    out: list[tuple[str, dict[str, str]]] = []
    for doc in yaml.safe_load_all(_helm_template()):
        if not isinstance(doc, dict) or doc.get("kind") not in ("Deployment", "StatefulSet", "Job", "CronJob"):
            continue
        spec = doc.get("spec") or {}
        template = (spec.get("jobTemplate") or {}).get("spec", {}).get("template") if doc["kind"] == "CronJob" else spec.get("template")
        annotations = ((template or {}).get("metadata") or {}).get("annotations") or {}
        if TOKEN_ANNOTATION in annotations:
            out.append((f"{doc['kind']}/{doc['metadata']['name']}", annotations))
    return out


def test_the_walk_finds_pods_given_a_token() -> None:
    """Without this the assertion below passes by finding nothing to check."""
    given = _pods_given_a_token()
    assert len(given) >= 10, f"only {len(given)} pods carry {TOKEN_ANNOTATION} — the render or this walk is broken"


def test_every_such_pod_carries_the_checksum_that_makes_a_rotation_restart_it() -> None:
    """Otherwise the rotation control cannot fire, and nothing reports that it did not.

    Measured live 2026-09-23 before the duplicate-key fix: four lakehouse Deployments carried 15 pod
    annotations, every one of them `dapr.io/*`, and no checksum — so rotating the dedicated service
    tokens left a pod holding the old credential until it was restarted by hand.
    """
    without = [name for name, annotations in _pods_given_a_token() if CHECKSUM_ANNOTATION not in annotations]

    assert not without, f"these pods are handed an app-token Secret with no {CHECKSUM_ANNOTATION}, so a rotation never restarts them: {without}"
