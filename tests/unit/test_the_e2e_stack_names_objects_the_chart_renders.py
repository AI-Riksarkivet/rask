"""A stack script may only address Kubernetes objects the chart actually renders.

`scripts/e2e_stack.sh` port-forwarded `svc/$RELEASE-rustfs` and scaled `deploy/$RELEASE-rustfs`. The
chart has rendered the object store as StatefulSet and Service `rask-minio` since 2026-09-11, so
neither existed. The port-forward failed quietly and the chaos drill's scale-to-zero was a no-op
against a missing Deployment — which means the leg that proves "a write with no object store must not
answer 200" was asserting against an object store that had never been taken down.

THE ROT IS SILENT ON BOTH SIDES. `kubectl port-forward` to a missing service logs and carries on;
`kubectl scale` on a missing Deployment is caught by the `|| true` that exists for a different reason.
So a rename in the chart leaves these scripts addressing nothing, and the only symptom is a proof that
passes without proving.

DERIVED FROM THE RENDER, never a list. The chart is the source of truth for what exists, so this reads
the rendered manifests and requires every `<kind>/$RELEASE-<name>` the scripts name to be one of them.
A twelfth workload renames tomorrow and this fails the same day.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = ("scripts/e2e_stack.sh", "scripts/ray_e2e_stack.sh")
#: Manifests the stack scripts `kubectl apply`, checked for the same class of rot by a different
#: shape: a plain YAML cannot reference chart values, so every cluster DNS name it hard-codes is a
#: copy of something the chart already renders. `ray-lance-demo.yaml` pointed Ray's object store at
#: `rask-rustfs-io`, which the chart has not rendered since the store moved to `rask-minio`.
_APPLIED = ("deploy/ray-lance-demo.yaml",)
_SERVICE_HOST = re.compile(r"https?://(rask-[a-z0-9-]+)[:/]")
#: `kubectl`'s short names, mapped to the `kind:` a rendered manifest declares.
_KINDS = {"svc": "Service", "service": "Service", "deploy": "Deployment", "deployment": "Deployment", "statefulset": "StatefulSet", "sts": "StatefulSet"}
_REFERENCE = re.compile(r"\b(svc|service|deploy|deployment|statefulset|sts)/\$\{?RELEASE\}?-([a-z0-9-]+)")


@pytest.fixture(scope="module")
def rendered() -> set[tuple[str, str]]:
    """`(kind, name)` for everything the chart renders, with the release named `rask`.

    Rendered with the same values `make k3s-up` uses for the two gates the chart refuses without, so
    this is the estate's real shape rather than a partial template pass.
    """
    done = subprocess.run(
        [
            "helm",
            "template",
            "rask",
            "chart/",
            "--set",
            "image.localImages=true",
            "--set",
            "frontend.oidc.sessionSecret=0123456789abcdef0123456789abcdef",
            "--set",
            "frontend.oidc.publicIssuer=http://dex.local:5556",
            "--set",
            "frontend.oidc.publicOrigin=http://rask.local",
            "--set",
            "frontend.oidc.clientSecret=abcdef0123456789abcdef0123456789",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip(f"helm could not render the chart here: {done.stderr.strip()[:200]}")
    out: set[tuple[str, str]] = set()
    for doc in yaml.safe_load_all(done.stdout):
        if isinstance(doc, dict) and doc.get("kind") and isinstance(doc.get("metadata"), dict):
            out.add((str(doc["kind"]), str(doc["metadata"].get("name"))))
    return out


def test_the_render_produced_something_to_compare_against(rendered: set[tuple[str, str]]) -> None:
    """Anti-vacuity: an empty render would make every assertion below pass on a chart that renders
    nothing at all, which is the exact shape of a gate that cannot fail."""
    assert len(rendered) > 20, f"the chart rendered {len(rendered)} objects — the parse or the render is broken"
    assert ("StatefulSet", "rask-minio") in rendered, "the object store is not in the render, so the comparison below means nothing"


@pytest.mark.parametrize("rel", _SCRIPTS)
def test_every_object_the_script_addresses_is_one_the_chart_renders(rel: str, rendered: set[tuple[str, str]]) -> None:
    path = _ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} is gone")
    referenced = {(_KINDS[short], f"rask-{name}") for short, name in _REFERENCE.findall(path.read_text(encoding="utf-8"))}
    assert referenced, f"{rel} addresses no release-scoped object — this gate is measuring something that moved"

    missing = sorted(referenced - rendered)
    assert not missing, (
        f"{rel} addresses objects the chart does not render: {missing}. A port-forward to a missing service "
        "carries on and a scale of a missing workload is a no-op, so the proof around them passes without proving."
    )


@pytest.mark.parametrize("rel", _APPLIED)
def test_a_manifest_the_stack_applies_names_services_the_chart_renders(rel: str, rendered: set[tuple[str, str]]) -> None:
    """A hard-coded cluster DNS name in an applied manifest is a copy of the chart's own render.

    It fails the way every name-rot in this estate fails: the pod starts, the endpoint does not
    resolve, and the symptom appears somewhere else entirely — here, as Ray unable to reach its object
    store part-way through a cascade.
    """
    path = _ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} is gone")
    services = set(_SERVICE_HOST.findall(path.read_text(encoding="utf-8")))
    assert services, f"{rel} names no in-cluster service — this gate is measuring something that moved"

    missing = sorted(name for name in services if ("Service", name) not in rendered)
    assert not missing, f"{rel} points at services the chart does not render: {missing}"
