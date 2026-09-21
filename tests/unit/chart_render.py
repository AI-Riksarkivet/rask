"""One cached `helm template` the chart gates can share.

WHY A SEAM AND NOT A FIFTEENTH SUBPROCESS. [[XC-037]] counts ~20 test files that each roll their own
`subprocess` render; rendering the chart costs a process and ~2.3 MB of YAML, and the suite pays it
once per caller. A gate added with its own private helper makes that row grow while it is open, so
this module exists for new gates to render THROUGH and for the existing ones to migrate onto.

CACHED ON THE VERBATIM FLAG TUPLE, which is what makes sharing worth anything: two gates asking for the
same overlay get one process between them. `functools.cache` is per-process, so under `-n 16
--dist loadfile` each worker renders once — still N processes instead of N x callers.

PARSED WITH `chart_yaml.FAST_LOADER` for the reason that module states: ~57% of `tests/unit`'s wall
clock was attributed to the pure-Python parser, and the C loader returns equal documents.
"""

from __future__ import annotations

import functools
import pathlib
import shutil
import subprocess

import pytest
import yaml

from tests.unit.chart_yaml import FAST_LOADER


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The fail-closed OIDC guards' dummy inputs. Without them `frontends.yaml` refuses to render at all,
#: so every caller needs them and none of them is testing OIDC.
OIDC_ARGS: tuple[str, ...] = (
    "--set-string", "frontend.oidc.sessionSecret=ci-dummy-session-secret-at-least-32-chars",
    "--set-string", "frontend.oidc.publicIssuer=https://auth.example.com/dex",
    "--set-string", "frontend.oidc.publicOrigin=https://lance.example.com",
)  # fmt: skip

#: The local-development overlay: side-loaded images, MinIO on.
DEFAULT_ARGS: tuple[str, ...] = ("--set", "image.localImages=true", "--set", "minio.enabled=true")


@functools.cache
def render(*extra: str) -> tuple[dict, ...]:
    """Every mapping document the chart renders under `OIDC_ARGS` plus `extra`.

    Returns a tuple rather than a list because the result is cached and shared: a caller that sorted or
    popped a list in place would corrupt the next gate's render.
    """
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(REPO / "chart"), *OIDC_ARGS, *extra]
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    return tuple(doc for doc in yaml.load_all(out, Loader=FAST_LOADER) if isinstance(doc, dict))


def containers(docs: tuple[dict, ...]) -> list[tuple[str, str, dict]]:
    """`(workload, container_name, container)` for every first-class workload shape.

    Init containers are included: they run in the same cgroup and against the same limit, so a baseline
    that skipped them would exempt exactly the containers that do the heavy one-shot work.
    """
    found: list[tuple[str, str, dict]] = []
    for doc in docs:
        kind, name = doc.get("kind"), doc.get("metadata", {}).get("name", "?")
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
            spec = doc["spec"]["template"]["spec"]
        elif kind == "CronJob":
            spec = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        else:
            continue
        for key in ("initContainers", "containers"):
            for container in spec.get(key) or []:
                found.append((f"{kind}/{name}", container["name"], container))
    return found


def env_of(container: dict) -> dict[str, str]:
    """The container's `name: value` env pairs. `valueFrom` entries are omitted deliberately — a gate
    reading a rendered literal cannot see what a secret reference resolves to at run time."""
    return {e["name"]: e["value"] for e in container.get("env") or [] if "value" in e}
