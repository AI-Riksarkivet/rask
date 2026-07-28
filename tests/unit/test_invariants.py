"""CLAIM-LINT — the mechanical guards for the bug class that kept escaping this repo (GOAL-prove-it P0.2).

Three real, shipped bugs motivated each guard below. Every one passed the entire unit + integration suite
and a manual "live-verified" run, because a prose CLAIM was never mechanically checked:

  1. "#4: every lineage publish is staged through the outbox" — THREE publishers bypassed it (the media
     head + two FAIL emits, one on a _DROP path where a lost publish erased the failure forever). I had
     verified the ONE publisher I changed and never grepped for the rest.
  2. "MEDALLION_LINEAGE_OUTBOX_URI is wired" — the chart injected it and no code ever read it (a dead env).
     A whole feature was configured and inert.
  3. "seed_warehouse grants the FGA parent edge" — it wrote `warehouse#parent`, a relation the warehouse
     type does NOT define (its pointer is `project`). OpenFGA rejected the write → a live 503, while every
     unit test stayed green because mocked `fga.check`/`write_tuples` pin the STRING, never the SCHEMA.

The rule these encode: a claim that cannot be proven by a grep, a test, or a render is not a claim — it is
a guess. Each test below fails on the ORIGINAL buggy code and passes now.
"""

from __future__ import annotations

import json
import pathlib
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
SERVICES = REPO / "services"
# The rask merge converted every service to src-layout (services/<n>/src/<n>/…) and merged the
# shared lib into service-kit (packages/service-kit/src/service_kit). rglob("*.py") over SERVICES
# still sees every service module; these two helpers resolve the paths the assertions name.
SERVICE_KIT = REPO / "packages" / "service-kit" / "src" / "service_kit"


def _svc(name: str) -> pathlib.Path:
    """Source root of a copied lance-ns service under the src-layout."""
    return SERVICES / name / "src" / name


CHART = REPO / "chart"


# --------------------------------------------------------------------------------------------------
# 1. #4 outbox uniformity — zero bare publishes to the LINEAGE topic
# --------------------------------------------------------------------------------------------------


def _bare_lineage_publishes() -> list[str]:
    """Every `dapr_publish.publish_event(...)` whose topic_name is `settings.lineage_topic`.

    A lineage event MUST go through `outbox.publish_lineage_with_outbox` (stage → publish → drop) so a
    crash between the Lance commit and the publish ack leaves the event recoverable. A bare publish is
    the exact commit→publish loss window #4 exists to close. TRIGGER topics (pub_topic / media_topic /
    bronze_topic / train_topic) are correctly bare: the outbox re-ingests lineage, it never re-fires triggers.
    """
    offenders: list[str] = []
    for py in SERVICES.rglob("*.py"):
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            if "dapr_publish.publish_event(" not in line:
                continue
            # the topic kwarg sits within the call's next few lines
            window = "\n".join(lines[i : i + 8])
            if "topic_name=settings.lineage_topic" in window:
                offenders.append(f"{py.relative_to(REPO)}:{i + 1}")
    return offenders


def test_every_lineage_publish_goes_through_the_outbox() -> None:
    offenders = _bare_lineage_publishes()
    assert not offenders, (
        "#4 claims 'every lineage publish is staged', but these publish to the LINEAGE topic WITHOUT the "
        f"outbox — a crash after the Lance commit loses the event forever: {offenders}. "
        "Use outbox.publish_lineage_with_outbox(...). (Trigger topics may stay bare.)"
    )


# --------------------------------------------------------------------------------------------------
# 2. No dead config — every env var the chart injects is actually READ by some service
# --------------------------------------------------------------------------------------------------

# Envs consumed by a THIRD-PARTY container's own binary, never by first-party code. Each entry is a
# server we deploy but did not write (RustFS/OpenBao/Postgres/NATS/Dapr/OTel/Greptime/Vector/Ray/OpenFGA),
# so its absence from our source proves nothing. Anything NOT matching here is OURS and must be read.
_THIRD_PARTY_ENV = re.compile(
    r"^(DAPR_|OTEL_|PYTHON|PATH$|HOME$|BAO_|VAULT_|POSTGRES_|PG|NATS_|AWS_|RUSTFS_|RUST_|GREPTIME|"
    r"VECTOR_|OPENFGA_|RAY_|MINIO_|S3_|TZ$|LANG$|LC_|UV_|VIRTUAL_ENV)"
)


def _chart_injected_envs() -> set[str]:
    envs: set[str] = set()
    for tpl in (CHART / "templates").rglob("*.yaml"):
        for m in re.finditer(r"name:\s*([A-Z][A-Z0-9_]{3,})\b", tpl.read_text()):
            name = m.group(1)
            if not _THIRD_PARTY_ENV.match(name):
                envs.add(name)
    return envs


def _first_party_source() -> str:
    """ALL first-party source — python services AND the SvelteKit frontend.

    Deliberately wider than services/: the BFF reads LINEAGE_API in TypeScript, so a services-only search
    would call a live, load-bearing env "dead". Searching every first-party file makes the guard STRONGER
    (it now covers the frontend too) rather than weaker. Pydantic reads envs via alias="FOO", so a plain
    substring match over source is the right check — not os.environ lookups.
    """
    parts = [p.read_text(errors="ignore") for p in SERVICES.rglob("*.py")]
    # The shared platform lib is first-party but lives OUTSIDE services/ after the rask merge
    # (packages/service-kit, packages/ratch). service_kit/media/config.py is where most
    # MEDIA_*/LANCE_* envs are actually read, so omitting packages/ would report live config as dead.
    pkgs = REPO / "packages"
    if pkgs.exists():
        parts += [p.read_text(errors="ignore") for p in pkgs.rglob("*.py")]
    # The model runners (runners/<name>/) are first-party too — the chart injects their env
    # (ASSIST_FRAME_BASE) and only runner code reads it, so excluding them would flag live
    # config as dead.
    runners = REPO / "runners"
    if runners.exists():
        parts += [p.read_text(errors="ignore") for p in runners.rglob("*.py")]
    fe = REPO / "frontend"
    if fe.exists():
        for ext in ("*.ts", "*.svelte", "*.js"):
            parts += [p.read_text(errors="ignore") for p in fe.rglob(ext) if "node_modules" not in p.parts and ".svelte-kit" not in p.parts]
    return "\n".join(parts)


def test_no_dead_chart_env_vars() -> None:
    """A chart-injected env that NO first-party code reads = a feature configured but INERT.

    This is exactly how MEDALLION_LINEAGE_OUTBOX_URI shipped: the chart set it, the producer never read
    it, and the outbox silently did nothing on that path while the docs claimed coverage. The feature was
    fully "configured" and completely dead.
    """
    source = _first_party_source()
    dead = sorted(env for env in _chart_injected_envs() if env not in source)
    assert not dead, (
        f"the chart injects these env vars but NO first-party code reads them (dead config → a feature "
        f"that is configured but inert): {dead}. Either wire them up or delete them from the chart."
    )


# --------------------------------------------------------------------------------------------------
# 3. FGA schema contract — every relation the code WRITES or CHECKS must exist on that type
# --------------------------------------------------------------------------------------------------


def _model_relations() -> dict[str, set[str]]:
    model = json.loads((SERVICE_KIT / "governed/auth/model.json").read_text())
    return {t["type"]: set(t.get("relations") or {}) for t in model["type_definitions"]}


def _fga_literals() -> list[tuple[str, str, str]]:
    """(file:line, object_type, relation) for every literal FGA (type, relation) pair in the code.

    Catches the `warehouse#parent` class: a mocked `fga.check`/`write_tuples` asserts the STRING that was
    passed, never that the relation EXISTS on the type — so a phantom relation sails through every unit
    test and only fails at runtime, as an OpenFGA rejection (a fail-closed 503 for every caller).
    """
    found: list[tuple[str, str, str]] = []
    # relation="X", ... obj=f"type:{...}"  /  ClientTuple(relation="X", object=f"type:...")
    rel_re = re.compile(r'relation=["\']([a-z_]+)["\']')
    obj_re = re.compile(r'(?:obj|object)=f?["\']([a-z_]+):')
    for py in SERVICES.rglob("*.py"):
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            rel = rel_re.search(line)
            if not rel:
                continue
            window = "\n".join(lines[max(0, i - 4) : i + 5])
            for obj in obj_re.finditer(window):
                found.append((f"{py.relative_to(REPO)}:{i + 1}", obj.group(1), rel.group(1)))
    return found


def test_every_fga_relation_in_code_exists_in_the_compiled_model() -> None:
    model = _model_relations()
    phantom = [f"{loc} -> {obj_type}#{rel}" for loc, obj_type, rel in _fga_literals() if obj_type in model and rel not in model[obj_type]]
    assert not phantom, (
        "the code writes/checks FGA relations that do NOT exist on that type in the compiled model.json — "
        f"OpenFGA REJECTS these at runtime (fail-closed 503 for every caller): {phantom}"
    )


def _helm_template(*set_values: str) -> str:
    """Render the chart, skipping the test if helm is not on PATH or in .localbin."""
    import shutil
    import subprocess

    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", str(CHART)]
    for value in set_values:
        argv += ["--set", value]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def test_every_first_party_deployment_is_hardened() -> None:
    """The docs claim "every Deployment has probes + preStop". The gateway had NEITHER (audit 2026-07-14).

    An "every" claim in prose is worth nothing; this loop is what makes it true. It renders the chart and
    checks each FIRST-PARTY Deployment (third-party subcharts — dapr/nats/openfga/dex — are not ours to
    template). preStop matters most on the gateway: it is the INGRESS, so without a drain delay a rolling
    update drops in-flight requests while kube-proxy is still routing to the terminating pod.
    """
    rendered = _helm_template()

    first_party = (
        "gateway", "catalog", "lineage", "compaction", "lance-ray",
        "bronze-to-silver", "silver-to-gold", "media-to-silver", "web",
    )  # fmt: skip
    unhardened: list[str] = []
    for doc in rendered.split("\n---"):
        if "kind: Deployment" not in doc:
            continue
        m = re.search(r"^\s*name:\s*(\S+)", doc, re.MULTILINE)
        name = m.group(1) if m else "?"
        if not any(f in name for f in first_party):
            continue
        missing = [k for k in ("livenessProbe", "readinessProbe", "preStop") if k not in doc]
        if missing:
            unhardened.append(f"{name} missing {missing}")
    assert not unhardened, f"first-party Deployments are not hardened: {unhardened}"


def test_ingress_holds_a_live_stream_open_longer_than_nginx_default() -> None:
    """A `query.live` stream dies at the edge, silently, unless the Ingress overrides the read timeout.

    ingress-nginx's default `proxy_read_timeout` is 60s and it measures IDLE time on the upstream
    connection, so a live query that yields only when its data changes — the discipline the lakehouse
    zone's `controlEvents` generator follows — is cut off after a minute of quiet. SvelteKit's SSE
    transport adds no keepalive to refresh that clock (kit 2.70.1 `runtime/server/remote.js` enqueues
    the payload and nothing else), so the browser reconnects every 60s and re-runs the generator from
    its first poll. That is more traffic than the `setInterval` it replaced, while looking live.

    Rendered, not asserted in prose: the annotation must be present AND longer than the 60s default,
    or the override is decoration.
    """
    rendered = _helm_template("ingress.enabled=true")
    ingress = next((doc for doc in rendered.split("\n---") if re.search(r"^kind: Ingress$", doc, re.MULTILINE)), None)
    assert ingress is not None, "ingress.enabled=true rendered no Ingress"
    m = re.search(r"nginx\.ingress\.kubernetes\.io/proxy-read-timeout:\s*\"?(\d+)\"?", ingress)
    assert m, (
        "the Ingress carries no nginx.ingress.kubernetes.io/proxy-read-timeout — every live stream "
        "through the edge is severed after nginx's 60s default, and SvelteKit sends no keepalive"
    )
    assert int(m.group(1)) > 60, (
        f"proxy-read-timeout is {m.group(1)}s, which is not longer than nginx's 60s default — the annotation is present but changes nothing"
    )


@pytest.mark.parametrize(
    ("obj_type", "relation"),
    [
        # the exact pairs whose absence caused live outages / dead gates
        ("warehouse", "project"),  # the parent pointer (NOT `parent` — that was the 503)
        ("warehouse", "owner"),
        ("project", "can_create_warehouse"),  # the dormant admin gate #3-A finally enforces
        ("namespace", "parent"),
        ("table", "parent"),
    ],
)
def test_load_bearing_relations_are_defined(obj_type: str, relation: str) -> None:
    assert relation in _model_relations()[obj_type], f"{obj_type}#{relation} is load-bearing but missing from the compiled model"


def test_every_helm_set_key_in_our_scripts_exists_in_values() -> None:
    """A `--set` key that does not exist in values.yaml is a SILENT no-op — helm accepts it without a word.

    The bug this encodes (2026-07-14): `scripts/e2e_stack.sh` passed `--set web.enabled=false` to deploy a
    headless stack. There was no `web.enabled` key. Helm shrugged, the web Deployment (which had no `if`
    guard at all) rendered anyway, its image is never built in CI, so it sat in ImagePullBackOff and
    `helm --wait` could never converge. The e2e-stack job — the entire point of P0.1, the job whose whole
    purpose is to stop us shipping unproven claims — therefore FAILED ON EVERY RUN and nobody noticed.

    A flag you *believe* you are setting, that silently sets nothing, is worse than no flag: it makes a
    stack you never actually configured look configured. This asserts every key we --set actually exists.
    """
    import yaml

    values = yaml.safe_load((CHART / "values.yaml").read_text())

    def defined(dotted: str) -> bool:
        node = values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True

    # `--set a.b=c` / `--set-json a.b=[...]` / `--set a.b=c,d.e=f` across every script we ship.
    pattern = re.compile(r"--set(?:-json|-string)?[= ]\"?([A-Za-z0-9_.\[\]-]+)=")
    unknown: list[str] = []
    for script in sorted((REPO / "scripts").glob("*.sh")):
        for key in pattern.findall(script.read_text()):
            base = key.split("[")[0]  # list index: catalog.multibase.dataBases[0] -> ...dataBases
            if not defined(base):
                unknown.append(f"{script.name}: --set {key} (no such key in values.yaml)")
    assert not unknown, "helm --set keys that silently do nothing:\n  " + "\n  ".join(unknown)


def test_no_warehouse_bucket_access_bypasses_the_deactivation_gate() -> None:
    """SECURITY INVARIANT (audit #2/#6 + #35 class): reaching a warehouse's isolated bucket connection —
    which happens only via ``_namespace_for_root`` — MUST consult the warehouse's lifecycle status, or a
    handler can provision/read inside a QUARANTINED (deactivated) bucket, bypassing tenant offboarding.

    Today two paths reach a bucket: ``get_namespace`` (through ``_resolve_warehouse_root``'s live status
    gate) and ``create_warehouse_namespace`` (which checks ``record["status"]`` inline). This test fails the
    moment a NEW caller of ``_namespace_for_root`` appears in a module that does not also gate on status —
    exactly the bug the audit found in the namespace-create path.
    """
    # Match the cached wrapper `_namespace_for_root(` but NOT the raw builder `build_namespace_for_root(`
    # (the wrapper's substring lives inside the builder's name) — a word boundary before the underscore.
    caller_re = re.compile(r"(?<![A-Za-z_])_namespace_for_root\(")
    ungated: list[str] = []
    for path in SERVICES.rglob("*.py"):
        text = path.read_text()
        if not caller_re.search(text):
            continue
        # The definition site (dependencies.py) gates via _resolve_warehouse_root; every caller must gate on
        # the warehouse status one way or another before it reaches the bucket.
        gated = "_resolve_warehouse_root" in text or "warehouse_status" in text or re.search(r'\.get\("status"\)|\["status"\]', text) is not None
        if not gated:
            ungated.append(str(path.relative_to(REPO)))
    assert not ungated, (
        "these modules reach a warehouse bucket via _namespace_for_root WITHOUT a deactivation-status gate "
        f"— a quarantined-warehouse bypass (audit #2/#6): {ungated}"
    )


def test_catalog_authz_primitive_fails_closed_on_openfga_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """SECURITY INVARIANT (condition 3a): the shared catalog authz primitive ``_require`` — which EVERY
    catalog gate (``authorize`` / ``require_*``) funnels through to ``fga.check`` — must RAISE (fail closed)
    when OpenFGA is unreachable, never swallow the outage and allow. If it failed OPEN, every gated route
    would too. (The lineage read gate's fail-closed is pinned in test_lineage_auth.py.)
    """
    import asyncio
    from unittest.mock import MagicMock

    from catalog.api import fga_deps as cat_fga
    from lance_namespace import ServiceUnavailableError

    async def _outage(*_a: object, **_k: object) -> bool:
        raise ServiceUnavailableError("openfga down")

    # Patch the fga module AS SEEN BY fga_deps (the consuming module), so the outage reaches
    # the exact `fga.check` reference `_require` calls.
    monkeypatch.setattr(cat_fga.fga, "check", _outage)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(cat_fga._require(MagicMock(), user="u", relation="can_read_data", obj="table:x"))


def test_authz_decisions_are_audited() -> None:
    """Compliance invariant (#41): the single authz choke point ``fga_deps._require`` must emit an audit
    event, so every governed access decision — allow and deny — lands on the dedicated audit trail. Before
    #41 only denials were logged (to the general logger). Grep-provable so it can never silently regress:
    the moment ``_require`` stops calling ``audit(``, a governed deployment's audit trail goes half-blind."""
    src = (_svc("catalog") / "api" / "fga_deps.py").read_text()
    body = src.split("async def _require(", 1)[1].split("\nasync def ", 1)[0]
    assert "audit(" in body, "_require must emit an audit event for every authz decision (#41 compliance)"


def test_batch_authz_and_credential_vending_are_audited() -> None:
    """Compliance invariant (#41 follow-up): the two authz surfaces that do NOT funnel through ``_require``
    — the batch gate and the credential vend — must emit their own audit events, or their decisions fall
    off the trail exactly the way the pre-#41 code's did. Grep-provable like the ``_require`` guard."""
    fga_src = (_svc("catalog") / "api" / "fga_deps.py").read_text()
    batch = fga_src.split("async def _authorize_batch(", 1)[1].split("\nasync def ", 1)[0]
    assert batch.count("audit(") >= 3, "_authorize_batch must audit table/parent/owner decisions (#41)"
    vend = (_svc("catalog") / "api" / "v1" / "endpoints" / "credentials.py").read_text()
    assert vend.count("audit(") >= 2, "credential vending must audit the write-tier gate + issuance (#41)"


# --------------------------------------------------------------------------------------------------
# 4. Event-fabric contract (DATA-CONTRACT §7) — topics are pinned constants, never inline literals
# --------------------------------------------------------------------------------------------------

#: The exact topic constants the event fabric runs on (DATA-CONTRACT §7.2). The topic NAME is the
#: compatibility unit — a consumer subscribed to `lineage.events.v1` is entitled to that payload shape
#: forever — so a rename or version bump must be a deliberate act that also updates this pin (and, for a
#: breaking change, ships a NEW `.vN` topic with parallel consumers), never a drive-by edit.
_PINNED_TOPICS: list[tuple[str, str]] = [
    # the two cross-plane topics carry an explicit .v1 (the versioned compatibility unit)
    ("packages/service-kit/src/service_kit/control_events.py", 'CONTROL_TOPIC = "catalog.control.v1"'),
    ("services/lineage/src/lineage/core/config.py", 'default="lineage.events.v1", alias="LINEAGE_DAPR_TOPIC"'),
    ("services/catalog/src/catalog/core/config.py", 'default="lineage.events.v1", alias="LANCE_DAPR_TOPIC"'),
    ("services/compaction/src/compaction/core/config.py", 'default="lineage.events.v1", alias="COMPACTION_LINEAGE_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="lineage.events.v1", alias="MEDALLION_LINEAGE_TOPIC"'),
    # the intra-cascade trigger topics (unversioned by design: both ends deploy atomically from one chart)
    ("services/medallion/src/medallion/core/config.py", 'default="medallion.bronze", alias="MEDALLION_SUB_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="medallion.bronze", alias="MEDALLION_BRONZE_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="training.jobs", alias="MEDALLION_TRAIN_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="medallion.media", alias="MEDALLION_MEDIA_TOPIC"'),
    # the stream bindings the topics land on (nats-stream-job) + the DLQ parking subjects
    ("chart/templates/nats-stream-job.yaml", 'add_if_missing CATALOG_CONTROL "catalog.control.>"'),
    ("chart/templates/nats-stream-job.yaml", 'add_if_missing DLQ "dlq.>"'),
    ("chart/templates/services.yaml", 'LINEAGE_DLQ_TOPIC, value: "dlq.lineage.events"'),
]


@pytest.mark.parametrize(("relpath", "needle"), _PINNED_TOPICS, ids=[n for _, n in _PINNED_TOPICS])
def test_event_topic_constants_are_pinned(relpath: str, needle: str) -> None:
    """DATA-CONTRACT §7.2 names these exact topics; this pin keeps the doc and the code from drifting."""
    assert needle in (REPO / relpath).read_text(), (
        f"{relpath} no longer contains `{needle}` — the event-fabric topic contract (DATA-CONTRACT §7.2) "
        "names this exact constant. A deliberate rename must update the doc + this pin together; a "
        "BREAKING payload change must instead add a NEW .vN topic with parallel consumers."
    )


def _inline_topic_publishes() -> list[str]:
    """Every `dapr_publish.publish_event(...)` call site whose `topic_name` is an (f-)string literal —
    or that has no `topic_name` kwarg in view at all — instead of a named settings field / constant.

    An inline literal is a topic name CI cannot see: it bypasses the pins above, the chart's env
    retargeting, and the versioning rule (DATA-CONTRACT §7.2). Every real site today passes
    `topic_name=settings.<x>` / `self._topic` / a plumbed-through parameter — this keeps it that way.
    """
    offenders: list[str] = []
    literal_re = re.compile(r"topic_name\s*=\s*f?[\"']")
    for py in SERVICES.rglob("*.py"):
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            if "dapr_publish.publish_event(" not in line:
                continue
            window = "\n".join(lines[i : i + 8])
            if literal_re.search(window) or "topic_name=" not in window:
                offenders.append(f"{py.relative_to(REPO)}:{i + 1}")
    return offenders


def test_every_publish_site_uses_a_named_topic_constant() -> None:
    offenders = _inline_topic_publishes()
    assert not offenders, (
        "these publish sites pass an inline topic string (or no topic_name kwarg) instead of a named "
        f"constant/settings field: {offenders}. Inline topics dodge the pinned-constant contract "
        "(DATA-CONTRACT §7.2) — route the name through config or a shared constant."
    )


def _direct_publish_event_calls() -> list[str]:
    """Every ``.publish_event(`` call site OUTSIDE the wrapper module ``service_kit/dapr_publish.py``.

    The wrapper exists because the Dapr SDK's ``publish_event`` has no per-call timeout and no default
    gRPC deadline — a wedged sidecar hangs the caller forever. ``dapr_publish.publish_event(...)`` is
    the wrapper itself (excluded by the lookbehind); a direct ``client.publish_event(...)`` reopens the
    hang the wrapper closes, so no first-party module outside the wrapper may make one.
    """
    wrapper = SERVICE_KIT / "dapr_publish.py"
    direct_re = re.compile(r"(?<!dapr_publish)\.publish_event\(")
    offenders: list[str] = []
    for py in SERVICES.rglob("*.py"):
        if py == wrapper:
            continue
        for i, line in enumerate(py.read_text().splitlines()):
            if direct_re.search(line):
                offenders.append(f"{py.relative_to(REPO)}:{i + 1}")
    return offenders


def test_every_publish_goes_through_the_timeout_wrapper() -> None:
    offenders = _direct_publish_event_calls()
    assert not offenders, (
        "these sites call .publish_event( directly instead of service_kit.dapr_publish.publish_event — the "
        f"unbounded SDK call a wedged sidecar hangs forever: {offenders}. Route the publish through the "
        "wrapper (it forwards **kwargs and enforces timeout_seconds)."
    )


def test_authentication_outcomes_are_audited() -> None:
    """Compliance invariant (#41): ``authenticate`` must audit both the success (who logged in) and the
    failure (rejected token) paths — authn was entirely unlogged before #41, so brute-force / forged-token
    attempts were invisible. Grep-provable: the failure + success audit calls must both remain."""
    src = (_svc("catalog") / "api" / "security.py").read_text()
    assert src.count("audit(") >= 2, "authenticate must audit both success and failure (#41 compliance)"
    assert "SUCCESS" in src and "FAILURE" in src


def test_user_state_store_default_matches_the_component_the_catalog_is_scoped_to() -> None:
    """The `/v1/user-state/*` routes work only if THREE facts agree, and none of them is in the code.

    The catalog's `user_state_store` default names a Dapr component; that component must exist; and the
    catalog app-id must be in its `scopes` — an unscoped app-id gets "component not found" from the
    sidecar and every user's saved work 503s. All three live in `chart/values.yaml`, which is not edited
    when someone renames a component or trims a scope list, so nothing else would notice. This renders the
    chart and checks the agreement.
    """
    from catalog.core.config import Settings

    default = Settings.model_fields["user_state_store"].default
    rendered = _helm_template()
    component = next(
        (
            doc
            for doc in rendered.split("\n---")
            if re.search(r"^kind: Component$", doc, re.MULTILINE) and re.search(rf"^  name: {re.escape(default)}$", doc, re.MULTILINE)
        ),
        None,
    )
    assert component is not None, (
        f"the catalog defaults LANCE_USER_STATE_STORE to {default!r}, but the chart renders no Dapr "
        "Component by that name — every /v1/user-state call would 503"
    )
    assert re.search(r"type: state\.", component), f"{default} is not a state store"
    scopes = component.split("scopes:", 1)
    assert len(scopes) == 2 and re.search(r"^\s+- catalog$", scopes[1], re.MULTILINE), (
        f"the catalog app-id is not in {default}'s scopes — the sidecar refuses to load the component "
        "for it, so per-subject user state is unreachable however correct the code is"
    )


# --------------------------------------------------------------------------------------------------
# 8. The 2026-07-26 mistake classes, as mechanical guards rather than resolutions
# --------------------------------------------------------------------------------------------------


def _uncommented(text: str) -> str:
    """Blank out Helm/YAML comments, keeping line numbers, so prose ABOUT a pattern is not a use of it."""
    text = re.sub(r"\{\{-?/\*.*?\*/-?\}\}", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.DOTALL)
    return "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())


def test_no_numeric_helm_default_can_swallow_an_explicit_zero() -> None:
    """`| default 255` rendered 255 for an explicit `0`, so a config change looked applied and nothing moved.

    Helm's `default` fires on any EMPTY value, and Go templates count `0` as empty. So every numeric knob
    written as `{{ .Values.x | default N }}` silently ignores the one value an operator is most likely to
    mean deliberately: `0` for "disabled", "unbounded", or "scaled to nothing". This cost a live debugging
    round on `frontend.idleTimeoutSeconds`, where `0` means "no connection lifetime cap" — exactly the
    value the live-stream fix needed — and where the rendered manifest kept saying 255.

    The correct idiom is `(hasKey $parent "key") | ternary $parent.key N`, which tests PRESENCE. This
    forbids the broken one chart-wide, because carving out exceptions is how the next instance gets in.
    """
    offenders = [
        f"{path.relative_to(REPO)}:{n}: {line.strip()}"
        for path in sorted((CHART / "templates").rglob("*"))
        if path.is_file()
        for n, line in enumerate(_uncommented(path.read_text()).splitlines(), 1)
        if re.search(r"\|\s*default\s+-?\d", line)
    ]
    assert not offenders, (
        "`| default <number>` treats an explicit 0 as absent, so the operator's value is silently "
        'replaced by the fallback. Use `(hasKey $parent "key") | ternary $parent.key <n>`:\n  ' + "\n  ".join(offenders)
    )


def test_no_pod_container_is_read_by_index() -> None:
    """`containerStatuses[0]` is the DAPR SIDECAR on a 2/2 pod, and its digest is identical everywhere.

    Reading index 0 to check whether a redeploy landed reported three services as unchanged when all three
    had in fact been replaced — the sidecar digest never moves, which should itself have been the tell.
    Any container read must name the container, e.g. `containerStatuses[?(@.name=="ray-head")]`.
    """
    roots = [REPO / "scripts", REPO / "Makefile", REPO / "tests", CHART]
    offenders = [
        f"{path.relative_to(REPO)}:{n}: {line.strip()}"
        for root in roots
        for path in ([root] if root.is_file() else sorted(root.rglob("*")))
        if path.is_file()
        and path.suffix in {"", ".sh", ".mjs", ".py", ".yaml", ".ts"}
        and path != Path(__file__)  # this file names the pattern in order to forbid it
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1)
        if "containerStatuses[0]" in line
    ]
    assert not offenders, (
        "a pod's containers are ordered by the injector, not by importance — index 0 is daprd on every "
        "sidecar-injected pod. Select by name instead:\n  " + "\n  ".join(offenders)
    )


def test_every_dapr_component_resolves_its_secrets_through_the_secret_store() -> None:
    """A password-bearing DSN was put in a k8s Secret — the exact anti-pattern `service_kit/governed/secrets.py` records.

    The estate's rule is that app-tier secrets come from OpenBao through the Dapr secret store as the sole
    source, fail-closed; k8s Secrets are for the infra tier (an owner's own credential, consumed by
    `secretKeyRef` in a pod spec). A Dapr Component is app tier. So a Component may reference a secret, but
    only by `secretKeyRef` WITH an `auth.secretStore` naming who resolves it — without that line Dapr looks
    in Kubernetes Secrets, which is how the anti-pattern comes back in while looking correct.

    It also forbids a literal credential in component metadata, which no `auth` block can redeem.
    """
    rendered = _helm_template()
    components = [d for d in rendered.split("\n---") if re.search(r"^kind: Component$", d, re.MULTILINE)]
    assert components, "the chart rendered no Dapr components — this guard would pass vacuously"

    for doc in components:
        name = re.search(r"^  name: (\S+)", doc, re.MULTILINE)
        label = name.group(1) if name else "<unnamed>"
        if "secretKeyRef" in doc:
            assert re.search(r"^auth:\s*$", doc, re.MULTILINE) and re.search(r"^\s+secretStore: \S", doc, re.MULTILINE), (
                f"component {label} uses secretKeyRef with no auth.secretStore, so Dapr resolves it from a Kubernetes Secret instead of OpenBao"
            )
        literal = re.search(r"value: \"?[a-z]+://[^\"\s]*:[^\"\s@]+@", doc)
        assert not literal, (
            f"component {label} carries a credential inline: {literal.group(0)[:60]}… — it must be a secretKeyRef resolved through the secret store"
        )
