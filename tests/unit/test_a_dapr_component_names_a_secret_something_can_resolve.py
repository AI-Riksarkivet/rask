"""A rendered Dapr Component may not reference a secret no configured store will resolve.

A Component's `secretKeyRef` is resolved by the store its `auth.secretStore` names. With none named,
Dapr falls back to Kubernetes Secrets — and this chart declares no `lance` Secret, so the load fails:

    Fatal error from runtime: failed to load components:
    rpc error: code = Unknown desc = Secret "lance" not found

THE BLAST RADIUS IS THE WHOLE RELEASE, which is what makes this worth a gate rather than a fix. A
component that fails to load kills the SIDECAR, so every app sharing it fails its health check for a
reason naming neither the component nor the flag behind it. Measured 2026-09-24 on `e2e-ray`, whose
overlay set `openbao.enabled=false`: `medallion-producer` and `bronze-to-silver` both died that way,
seven minutes into a deploy, and the visible symptom was `TimeoutError: Dapr health check timed out`.

IT WAS HIDDEN BEHIND AN EARLIER BUG. `auth:` used to render as YAML null, so the CRD refused the
Component outright and it never loaded — nobody reached the point of discovering the secret was not
there either. Fixing the null exposed this one underneath, which is the ordinary shape of a lane that
nothing has run: the failures come out in sequence, not all at once.

SCOPED TO THE OVERLAYS WE DEPLOY, and that bound is deliberate. `openbao.enabled=false` is a
legitimate render — the estate's own suites use it to exercise the non-Dapr secret paths, and an
operator may supply a `lance` Secret out of band, which is a reference and not a carried secret. So
the chart does not refuse the shape; this gate asserts it of the estates whose values we own, read
out of the stack scripts themselves. A first cut of this failed the RENDER instead and broke nine
tests that disable OpenBao on purpose: the combination is unsupported in OUR stacks, not unsound.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from tests.unit.chart_render import OIDC_ARGS, render
from tests.unit.test_no_chart_owned_manifest_renders_a_null import _overlays


REPO = pathlib.Path(__file__).resolve().parents[2]


def _secret_refs(node: object, found: list[dict]) -> list[dict]:
    if isinstance(node, dict):
        if isinstance(node.get("secretKeyRef"), dict):
            found.append(node["secretKeyRef"])
        for value in node.values():
            _secret_refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _secret_refs(value, found)
    return found


@pytest.mark.parametrize("label,overlay", _overlays(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_referenced_secret_has_a_store_to_resolve_it(label: str, overlay: tuple[str, ...]) -> None:
    try:
        docs = render(*overlay, *OIDC_ARGS)
    except subprocess.CalledProcessError as exc:
        # SURFACE HELM'S OWN WORDS. The chart REFUSES this combination at render, so an overlay that
        # reintroduces it fails here — and a bare CalledProcessError shows a 400-character argv and
        # none of the message written to explain the fix. Measured while mutation-checking this gate.
        pytest.fail(f"the {label} overlay no longer renders:\n{(exc.stderr or exc.stdout or '').strip()[:800]}")
    components = [doc for doc in docs if doc.get("kind") == "Component"]
    assert components, f"{label} rendered no Dapr Component — the gate lost its subject"
    offenders = []
    for doc in components:
        refs = _secret_refs(doc.get("spec") or {}, [])
        store = (doc.get("auth") or {}).get("secretStore")
        if refs and not store:
            names = sorted({str(ref.get("name")) for ref in refs})
            offenders.append(f"{doc['metadata']['name']} references {names} with no auth.secretStore")
    assert not offenders, (
        f"under the {label} overlay these Dapr Components name a secret nothing will resolve, so daprd "
        f"exits fatal on load and takes every app's sidecar with it:\n  " + "\n  ".join(offenders)
    )
