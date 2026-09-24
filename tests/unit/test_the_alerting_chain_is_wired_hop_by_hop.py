"""Every hop between a proven alert rule and a firing page must be gated, not just the ends.

Two proofs already exist and they cover the ENDS. `make alert-rules-check` runs promtool over
`chart/alerting/rules.yml` and proves the logic fires on synthetic series; `make alert-rules-drill`
replays every expression against a real GreptimeDB and proves the production engine accepts it.
Between them sits the delivery, and nothing looked at it: the file is copied into a ConfigMap by
`.Files.Get | indent 4`, mounted at a path the vmalert container names separately, and pointed at an
Alertmanager whose Service is rendered somewhere else again. Each of those is a place a rename or a
re-indent makes the rules silently absent — vmalert starts happily with no rules and pages nobody,
which is indistinguishable from an estate with nothing wrong.

MEASURED 2026-09-24: the rendered ConfigMap is 82,864 bytes carrying 13 groups and 52 rules, and
`observability.alerting.enabled` defaults FALSE — so on this estate nothing evaluates any of them
today. That is the reason these hops need a gate rather than an observation: they are not exercised
by the running system, so only a render can say whether they still line up.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[2]
_RULES = _ROOT / "chart" / "alerting" / "rules.yml"
_VALUES = [
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
    # The toggles under test. Both default false, which is exactly why this renders them explicitly:
    # a default render proves nothing about a chain nobody has switched on yet.
    "--set",
    "observability.enabled=true",
    "--set",
    "observability.alerting.enabled=true",
]


@pytest.fixture(scope="module")
def rendered() -> dict[tuple[str, str], dict]:
    done = subprocess.run(["helm", "template", "rask", "chart/", *_VALUES], cwd=_ROOT, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        pytest.skip(f"helm could not render the chart here: {done.stderr.strip()[:200]}")
    out: dict[tuple[str, str], dict] = {}
    for doc in yaml.safe_load_all(done.stdout):
        if isinstance(doc, dict) and doc.get("kind") and isinstance(doc.get("metadata"), dict):
            out[(str(doc["kind"]), str(doc["metadata"].get("name")))] = doc
    return out


def _source_rules() -> dict[str, str]:
    """`{alert name: expr}` as the proven file declares them."""
    parsed = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    return {r["alert"]: str(r["expr"]).strip() for g in parsed["groups"] for r in g["rules"]}


def test_the_source_file_declares_rules_at_all() -> None:
    """Anti-vacuity: every comparison below is against this set, and an empty one passes them all."""
    rules = _source_rules()

    assert len(rules) > 20, f"only {len(rules)} rules parsed out of chart/alerting/rules.yml"


def test_every_proven_rule_survives_the_render_into_the_configmap(rendered: dict[tuple[str, str], dict]) -> None:
    """Hop one: file -> ConfigMap. `.Files.Get | indent 4` is a text operation over a YAML document,
    so a rule can be lost or corrupted without the template failing."""
    cm = rendered.get(("ConfigMap", "rask-alert-rules"))
    assert cm is not None, "the alert-rules ConfigMap does not render with alerting enabled"

    body = cm["data"]["rules.yml"]
    shipped = {r["alert"]: str(r["expr"]).strip() for g in yaml.safe_load(body)["groups"] for r in g["rules"]}
    source = _source_rules()

    assert shipped.keys() == source.keys(), (
        f"the rendered rules differ from the proven file: only-file={sorted(source.keys() - shipped.keys())}, "
        f"only-render={sorted(shipped.keys() - source.keys())}"
    )
    differing = sorted(name for name, expr in source.items() if shipped[name] != expr)
    assert not differing, f"these rules' expressions changed passing through the render: {differing}"


def test_vmalert_reads_the_rules_where_they_are_actually_mounted(rendered: dict[tuple[str, str], dict]) -> None:
    """Hop two: ConfigMap -> container path. vmalert starts with no rules and pages nobody when the
    `-rule=` path and the mount disagree, which looks exactly like a healthy estate."""
    deploy = rendered.get(("Deployment", "rask-vmalert"))
    assert deploy is not None, "vmalert does not render with alerting enabled"

    pod = deploy["spec"]["template"]["spec"]
    container = pod["containers"][0]
    rule_args = [a for a in container.get("args", []) if a.startswith("-rule=")]
    assert len(rule_args) == 1, f"expected exactly one -rule= argument, found {rule_args}"
    rule_path = rule_args[0].split("=", 1)[1]

    mounts = {m["mountPath"]: m["name"] for m in container.get("volumeMounts", [])}
    holder = next((name for path, name in mounts.items() if rule_path.startswith(path.rstrip("/") + "/")), None)
    assert holder, f"-rule={rule_path} is under no volumeMount: {sorted(mounts)}"

    volume = next((v for v in pod.get("volumes", []) if v["name"] == holder), None)
    assert volume and volume.get("configMap", {}).get("name") == "rask-alert-rules", f"the volume behind {rule_path} is not the alert-rules ConfigMap: {volume}"
    key = rule_path.rsplit("/", 1)[1]
    assert key in rendered[("ConfigMap", "rask-alert-rules")]["data"], f"the ConfigMap has no key {key!r} for vmalert to read"


def test_vmalert_points_at_services_the_chart_renders(rendered: dict[tuple[str, str], dict]) -> None:
    """Hop three: vmalert -> the datasource it queries and the notifier it pages. Both are in-cluster
    names rendered by other templates, so either can be renamed out from under this one."""
    container = rendered[("Deployment", "rask-vmalert")]["spec"]["template"]["spec"]["containers"][0]
    args = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in container.get("args", []) if "=" in a}

    for flag in ("-datasource.url", "-notifier.url"):
        assert flag in args, f"vmalert has no {flag} — it queries or pages nothing"
        host = args[flag].split("//", 1)[1].split("/", 1)[0]
        name, _, port = host.partition(":")
        service = rendered.get(("Service", name))
        assert service is not None, f"{flag} names {name}, which the chart does not render"
        if port:
            ports = {str(p["port"]) for p in service["spec"]["ports"]}
            assert port in ports, f"{flag} uses port {port}; Service {name} exposes {sorted(ports)}"


def test_alertmanager_has_a_receiver_its_route_names(rendered: dict[tuple[str, str], dict]) -> None:
    """Hop four: a firing alert reaches a route that reaches a receiver. An empty `webhookUrl` is a
    deliberate black hole — Alertmanager still receives and groups — but a route naming a receiver
    that does not exist is a config Alertmanager refuses to load at all."""
    cm = rendered.get(("ConfigMap", "rask-alertmanager"))
    assert cm is not None, "alertmanager does not render with alerting enabled"

    config = yaml.safe_load(next(iter(cm["data"].values())))
    receivers = {r["name"] for r in config.get("receivers", [])}
    named = {config["route"]["receiver"], *(r["receiver"] for r in config["route"].get("routes", []) if "receiver" in r)}

    assert receivers, "alertmanager renders no receivers, so it cannot load its config"
    assert named <= receivers, f"the route names receivers that do not exist: {sorted(named - receivers)}"


def test_the_values_this_estate_DEPLOYS_actually_enable_the_chain() -> None:
    """The last hop, and the one the render above cannot see.

    Every assertion here renders with `--set observability.alerting.enabled=true`, which proves the
    chain lines up in a configuration nobody necessarily runs. `make k3s-up` deploys
    `chart/values-local.yaml`, so that file is what decides whether the 52 proven rules are evaluated
    by anything at all — and it is the difference between a resilience claim and a mechanism.

    `chart/values.yaml` stays false on purpose: an estate opts in.
    """
    local = yaml.safe_load((_ROOT / "chart" / "values-local.yaml").read_text(encoding="utf-8"))
    base = yaml.safe_load((_ROOT / "chart" / "values.yaml").read_text(encoding="utf-8"))

    assert local.get("observability", {}).get("alerting", {}).get("enabled") is True, (
        "chart/values-local.yaml does not enable alerting, so `make k3s-up` deploys no vmalert and "
        "nothing on this estate evaluates any of the rules this repo proves."
    )
    assert base.get("observability", {}).get("alerting", {}).get("enabled") is False, (
        "chart/values.yaml turned alerting on by default — every estate then grows two workloads it did not ask for."
    )
