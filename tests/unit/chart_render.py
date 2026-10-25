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

import copy
import functools
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

from tests.unit.chart_yaml import FAST_LOADER


REPO = pathlib.Path(__file__).resolve().parents[2]

#: The bus a render names ([[XC-129]]): NATS JetStream is a prerequisite rask never installs, so every client dials
#: this address.
NATS_URL = "nats://bus.example.com:4222"

#: The API server's address as a platform states it to the NetworkPolicy ([[XC-143]]): a documentation range, so no
#: other peer a render holds can be mistaken for it.
API_SERVER_CIDR = "192.0.2.10/32"

#: The platform's secret store a production render names ([[XC-149]]), and its address as the platform states it to the
#: NetworkPolicy: a documentation range no other peer shares.
SECRET_STORE_URL = "https://vault.example.com:8200"
SECRET_STORE_CIDR = "198.51.100.20/32"

#: The prerequisites a render names: the APIs the chart checks through `.Capabilities`, the External Secrets Operator
#: ([[XC-004]]) and Dapr ([[XC-128]]), the NATS bus's address ([[XC-129]]), and the platform's Gateway the routes attach
#: to (ADR 0165). Under `networkPolicy.enabled` the egress to
#: the API server, the telemetry backend and an external OpenFGA is the platform's to state too ([[XC-143]]). A cluster-less render names
#: the cluster it targets. Without them every render refuses.
PREREQ_ARGS: tuple[str, ...] = (
    "--api-versions", "external-secrets.io/v1/ExternalSecret",
    "--api-versions", "generators.external-secrets.io/v1alpha1/Password",
    "--api-versions", "dapr.io/v1alpha1",
    "--set-string", f"nats.url={NATS_URL}",
    "--set-json", 'route.parentRefs=[{"name":"edge","namespace":"edge-system"}]',
    "--set-json", f'networkPolicy.apiServerEgress=[{{"to":[{{"ipBlock":{{"cidr":"{API_SERVER_CIDR}"}}}}],"ports":[{{"protocol":"TCP","port":6443}}]}}]',
    "--set-json", 'observability.export.egress=[{"to":[{"ipBlock":{"cidr":"203.0.113.10/32"}}],"ports":[{"protocol":"TCP","port":4318}]}]',
    "--set-json", 'auth.fgaEgress=[{"to":[{"ipBlock":{"cidr":"198.51.100.10/32"}}],"ports":[{"protocol":"TCP","port":443}]}]',
)  # fmt: skip

#: The Ray an estate names ([[CP-041]]): with the cascade's Ray lane on (the default) and no in-cluster
#: head, a render that names no Ray dashboard refuses. An external one keeps every caller's render free
#: of a RayCluster it is not testing. A `--set` beats any `-f`, so a gate of the in-cluster derivation
#: renders without it.
RAY_ARGS: tuple[str, ...] = ("--set-string", "ray.dashboardUrl=http://ray.example.com:8265")

#: Observability ON with the backend an operator names ([[XC-127]]): off by default, it renders the Collector and the
#: fleet's OTLP export only with an endpoint to send to. A gate of a pod's telemetry wiring states it.
OBSERVABILITY_SETS: tuple[str, ...] = ("observability.enabled=true", "observability.export.endpoint=http://otlp.example.com:4318/v1/otlp")
OBSERVABILITY_ARGS: tuple[str, ...] = tuple(arg for value in OBSERVABILITY_SETS for arg in ("--set", value))

#: The fail-closed OIDC guards' dummy inputs plus the ESO APIs, naming no Ray: the base of a gate of the
#: Ray guard itself.
UNNAMED_RAY_ARGS: tuple[str, ...] = (
    "--set-string", "auth.oidc.issuer=https://auth.example.com/dex",
    *PREREQ_ARGS,
)  # fmt: skip

#: Every input the chart refuses to render without. Every caller needs them and none of them is testing
#: those guards.
OIDC_ARGS: tuple[str, ...] = (*UNNAMED_RAY_ARGS, *RAY_ARGS)

#: The local-development overlay: side-loaded images, the object store on.
DEFAULT_ARGS: tuple[str, ...] = ("--set", "image.localImages=true", "--set", "objectStore.enabled=true")

#: The production overlay with the inputs its fail-closed guards refuse to render without, the set
#: `scripts/prod_render_check.sh` supplies.
PROD_ARGS: tuple[str, ...] = (
    "-f", str(REPO / "deploy/values-prod.yaml"),
    "--set", "signing.provisioned=true",
    "--set", "nats.auth.provisioned=true",
    "--set-string", "observability.export.endpoint=http://otlp.example.com:4318/v1/otlp",
    "--set-string", "auth.fgaApiUrl=https://fga.example.com",
    "--set-json", 'auth.oidc.connectors={"ldap":"corp"}',
    "--set-string", "age.externalHost=pg.example.com",
    "--set-string", "objectStore.externalEndpoint=https://s3.example.com",
    "--set-string", f"openbao.externalAddr={SECRET_STORE_URL}",
    "--set-json", f'openbao.egress=[{{"to":[{{"ipBlock":{{"cidr":"{SECRET_STORE_CIDR}"}}}}],"ports":[{{"protocol":"TCP","port":8200}}]}}]',
    "--set", "backups.volumeSnapshot.snapshotClassName=csi-snapclass",
    # `--set-json`, so a gate's later `--set-json route.hostnames=[]` replaces it: helm applies every `--set` after every `--set-json`.
    "--set-json", 'route.hostnames=["lance.example.com"]',
    "--set", "image.repository=ghcr.io/example/rask",
)  # fmt: skip


def render_chart_text(chart: pathlib.Path, *extra: str, base: tuple[str, ...] = OIDC_ARGS) -> str:
    """Helm's raw stdout for the chart at ``chart`` under ``base`` (default `OIDC_ARGS`) plus ``extra``, uncached.

    For a gate that renders a modified COPY of the chart: no `--set` reaches a file the chart reads with `.Files.Get`,
    and no other gate can share a copy's render. A gate of one of the required inputs passes a ``base`` without it.
    """
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(chart), *base, *extra]
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


#: What the annotator's release ([[XC-167]], ADR 0171) refuses to render without: the APIs of its actors, its state Cluster
#: and its app token, and rask's issuer, which it accepts tokens from.
ANNOTATOR_FIXTURE_ARGS: tuple[str, ...] = (
    "--api-versions", "dapr.io/v1alpha1",
    "--api-versions", "postgresql.cnpg.io/v1/Cluster",
    "--api-versions", "generators.external-secrets.io/v1alpha1/Password",
    "--api-versions", "external-secrets.io/v1/ExternalSecret",
    "--set-string", "auth.oidc.issuer=https://auth.example.com/dex",
)  # fmt: skip


@functools.cache
def render_fixture(name: str, *extra: str) -> tuple[dict, ...]:
    """Every mapping document a development fixture (`deploy/fixtures/<name>`) renders, as `scripts/fixtures.sh` installs it.

    The rask chart installs no engine and no prerequisite (ADR 0153); `make k3s-up` installs them from fixtures, so a gate
    about what the deployed engine or bus carries reads the fixture's render. A fixture's own chart renders offline (its
    dependencies vendored by `make k3s-deps`); an upstream one (`REPO` in its fixture.env) is fetched from that repository.
    """
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not pathlib.Path(helm).exists():
        pytest.skip("helm not available")
    fixture = REPO / "deploy/fixtures" / name
    env = dict(line.split("=", 1) for line in (fixture / "fixture.env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    source = [env["CHART"], "--repo", env["REPO"], "--version", env["VERSION"]] if env.get("REPO") else [str(fixture / env["CHART"])]
    values = ["-f", str(fixture / "values.yaml")] if (fixture / "values.yaml").is_file() else []
    argv = [helm, "template", env["RELEASE"], *source, "--namespace", env["NAMESPACE"], *values, *extra]
    text = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    return tuple(doc for doc in yaml.load_all(text, Loader=FAST_LOADER) if isinstance(doc, dict))


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


def uvicorn_target(container: dict) -> str | None:
    """The `module:attribute` a container's uvicorn serves, read off its whole command line (`args` carry it, behind
    `uvicorn` or the tracer that execs it), or None for a container that runs no uvicorn."""
    argv = [str(part) for part in [*(container.get("command") or []), *(container.get("args") or [])]]
    if "uvicorn" not in argv:
        return None
    return next((part for part in argv if ":" in part and not part.startswith("-")), None)


def env_of(container: dict) -> dict[str, str]:
    """The container's `name: value` env pairs. `valueFrom` entries are omitted deliberately — a gate
    reading a rendered literal cannot see what a secret reference resolves to at run time."""
    return {e["name"]: e["value"] for e in container.get("env") or [] if "value" in e}


def selects(selector: dict, labels: dict[str, str]) -> bool:
    """Whether a label selector (`matchLabels`, `matchExpressions`) matches a pod's labels, as the API server reads it."""
    if any(labels.get(key) != value for key, value in (selector.get("matchLabels") or {}).items()):
        return False
    for expression in selector.get("matchExpressions") or []:
        key, operator, values = expression["key"], expression["operator"], expression.get("values") or []
        held = {"In": key in labels and labels[key] in values, "NotIn": labels.get(key) not in values, "Exists": key in labels}
        if not held.get(operator, key not in labels):
            return False
    return True


#: One NATS user as the dev OpenBao's seed issues it: `nats_user <name> '<publish,...>' '<subscribe,...>'`.
_NATS_USER = re.compile(r"^\s*nats_user ([a-z0-9][a-z0-9-]*) '([^']*)' '([^']*)'$", re.MULTILINE)
#: A stream the NATS stream Job creates, through either of its two helpers, never a commented-out one.
_STREAM = re.compile(r'^[^\S\n]*add(?:_workqueue)?_if_missing ([A-Z_]+) "([^"]+)"', re.MULTILINE)


def seed_scripts(docs: tuple[dict, ...]) -> dict[str, str]:
    """The dev store's seed scripts rask renders (`<release>-openbao-seed`: `mint.sh`, `open-and-seed.sh`); empty when it renders none."""
    found = [d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-openbao-seed")]
    return dict(found[0]["data"]) if found else {}


def store_identities(docs: tuple[dict, ...]) -> dict[str, str]:
    """The role and policy of every identity that reads the secret store, which rask renders for any store
    (`<release>-secret-store-identities`: `policy-<role>.json`, `role-<role>.json`); empty when it renders none."""
    found = [d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-secret-store-identities")]
    return dict(found[0]["data"]) if found else {}


def with_dev_store(docs: tuple[dict, ...]) -> tuple[dict, ...]:
    """rask's render plus the dev store that runs its seed: the openbao fixture's objects, each container that runs one of
    rask's scripts (`sh /seed/<script>`) given that script's text, as the pod runs it ([[XC-131]]). Unchanged when rask
    renders no seed."""
    scripts = seed_scripts(docs)
    if not scripts:
        return docs
    store = copy.deepcopy(render_fixture("openbao"))
    for workload in store:
        if workload.get("kind") != "Deployment":
            continue
        pod = workload["spec"]["template"]["spec"]
        for container in [*pod.get("initContainers", []), *pod["containers"]]:
            command = container.get("command") or []
            if len(command) == 2 and command[0] == "sh" and command[1].startswith("/seed/"):
                container["command"] = ["sh", "-c", scripts[command[1].rsplit("/", 1)[1]]]
    return (*docs, *store)


def nats_users(docs: tuple[dict, ...]) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Each NATS user the dev OpenBao's seed issues, as (publish, subscribe) permissions; empty when the render issues none.

    Read off the seed script rask renders, so it is the table as the chart passes it to nsc (values.yaml `nats.auth.users`).
    """
    issued: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
    for user, publish, subscribe in _NATS_USER.findall(seed_scripts(docs).get("open-and-seed.sh", "")):
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
