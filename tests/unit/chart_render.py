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
import re
import shutil
import subprocess

import pytest
import yaml

from tests.unit.chart_yaml import FAST_LOADER


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The APIs of the External Secrets Operator, a prerequisite the chart checks through `.Capabilities`
#: ([[XC-004]]): a cluster-less render names the cluster it targets. Without them every render refuses.
ESO_ARGS: tuple[str, ...] = (
    "--api-versions", "external-secrets.io/v1/ExternalSecret",
    "--api-versions", "generators.external-secrets.io/v1alpha1/Password",
)  # fmt: skip

#: The fail-closed OIDC guards' dummy inputs, plus the ESO APIs. Without them `frontends.yaml` refuses
#: to render at all, so every caller needs them and none of them is testing OIDC.
OIDC_ARGS: tuple[str, ...] = (
    "--set-string", "frontend.oidc.publicIssuer=https://auth.example.com/dex",
    "--set-string", "frontend.oidc.publicOrigin=https://lance.example.com",
    *ESO_ARGS,
)  # fmt: skip

#: The local-development overlay: side-loaded images, MinIO on.
DEFAULT_ARGS: tuple[str, ...] = ("--set", "image.localImages=true", "--set", "minio.enabled=true")


def render_chart_text(chart: pathlib.Path, *extra: str) -> str:
    """Helm's raw stdout for the chart at ``chart`` under `OIDC_ARGS` plus ``extra``, uncached.

    For a gate that renders a modified COPY of the chart: no `--set` reaches a file the chart reads with `.Files.Get`,
    and no other gate can share a copy's render.
    """
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(chart), *OIDC_ARGS, *extra]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


@functools.cache
def render_text(*extra: str) -> str:
    """Helm's RAW stdout for this overlay — the bytes Helm itself stores in the release Secret.

    Separate from `render` because parsing is lossy in the one dimension the size gate cares about:
    `yaml.safe_load` drops every comment, so a budget computed from re-serialized documents measures a
    manifest that does not exist and can never fail. Measured 2026-09-21, that mistake made the gate
    pass with three un-converted templates restored.
    """
    return render_chart_text(REPO / "chart", *extra)


@functools.cache
def render(*extra: str) -> tuple[dict, ...]:
    """Every mapping document the chart renders under `OIDC_ARGS` plus `extra`.

    Returns a tuple rather than a list because the result is cached and shared: a caller that sorted or
    popped a list in place would corrupt the next gate's render.
    """
    return tuple(doc for doc in yaml.load_all(render_text(*extra), Loader=FAST_LOADER) if isinstance(doc, dict))


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


#: One NATS user as the dev OpenBao's seed issues it: `nats_user <name> '<publish,...>' '<subscribe,...>'`.
_NATS_USER = re.compile(r"^\s*nats_user ([a-z0-9][a-z0-9-]*) '([^']*)' '([^']*)'$", re.MULTILINE)
#: A stream the NATS stream Job creates, through either of its two helpers, never a commented-out one.
_STREAM = re.compile(r'^[^\S\n]*add(?:_workqueue)?_if_missing ([A-Z_]+) "([^"]+)"', re.MULTILINE)


def nats_users(docs: tuple[dict, ...]) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Each NATS user the dev OpenBao's seed issues, as (publish, subscribe) permissions; empty when the render issues none.

    Read off the script the seed container runs, so it is the table as the chart passes it to nsc (values.yaml `nats.auth.users`).
    """
    issued: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for _, name, container in containers(docs):
        if name == "seed":
            for user, publish, subscribe in _NATS_USER.findall((container.get("command") or [""])[-1]):
                issued[user] = (frozenset(publish.split(",")), frozenset(subscribe.split(",")))
    return issued


def jetstream_streams(docs: tuple[dict, ...]) -> dict[str, str]:
    """Each stream the NATS stream Job creates -> the subject filter it captures."""
    found: dict[str, str] = {}
    for workload, _, container in containers(docs):
        if workload.startswith("Job/") and "-nats-stream-" in workload:
            found.update(_STREAM.findall((container.get("command") or [""])[-1]))
    return found


def captures(subject: str, declared: str) -> bool:
    """NATS subject matching for the two forms the stream Job declares: a `>` tail or a literal."""
    return subject.startswith(declared[:-1]) if declared.endswith(".>") else subject == declared
