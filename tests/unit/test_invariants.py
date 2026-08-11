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
from typing import Any

import pytest
import yaml


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
#
# `HF_` is the huggingface_hub/transformers family. `HF_HOME` in particular is load-bearing rather
# than decorative: the Ray image runs with a read-only root filesystem, so HF's default
# `~/.cache/huggingface` is unwritable and every model download would fail — the chart repoints it at
# a writable `/cache/hf` volume (`chart/templates/rayservice.yaml`). It is read by the HF libraries,
# never by our code, so its absence from first-party source proves nothing. Note `HOME$` above is
# anchored and matches only a var named exactly HOME, which is why HF_HOME needed its own prefix.
_THIRD_PARTY_ENV = re.compile(
    r"^(DAPR_|OTEL_|PYTHON|PATH$|HOME$|HF_|BAO_|VAULT_|POSTGRES_|PG|NATS_|AWS_|RUSTFS_|RUST_|GREPTIME|"
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
    #
    # `parent_object=` / `parent_relation=` are EXCLUDED from this naive pairing, because in
    # `grant_on_create` the parent is the tuple's USER, not its object: the tuple is
    # (user=<parent_object>, relation=<parent_relation>, object=<resource>:<obj_id>). Pairing them
    # here reported `project#tenant`, which is not a relation anyone writes. They get their own,
    # STRICTER rule below — the relation must exist on the RESOURCE type.
    rel_re = re.compile(r'(?<!parent_)relation=["\']([a-z_]+)["\']')
    obj_re = re.compile(r'(?<!parent_)(?:obj|object)=f?["\']([a-z_]+):')
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


def _parent_edge_literals() -> list[tuple[str, str, str]]:
    """(file:line, resource_type, parent_relation) for every `grant_on_create` parent edge.

    The edge's relation lives on the CHILD, so `parent_relation` must exist on `resource`. Every
    governed type spells it `parent`; `annotation_project` spells it `tenant`. Writing the wrong one
    produces a tuple OpenFGA ACCEPTS and no rule ever reads — the inheritance silently does not
    exist, and the object looks unowned by everyone including its creator. That failure is invisible
    to a mocked-client unit test, which is the same blind spot the sibling check above exists for.
    """
    found: list[tuple[str, str, str]] = []
    res_re = re.compile(r'resource=["\']([a-z_]+)["\']')
    prel_re = re.compile(r'parent_relation=["\']([a-z_]+)["\']')
    for py in SERVICES.rglob("*.py"):
        lines = py.read_text().splitlines()
        for i, line in enumerate(lines):
            prel = prel_re.search(line)
            if not prel:
                continue
            window = "\n".join(lines[max(0, i - 8) : i + 3])
            res = res_re.search(window)
            if res:
                found.append((f"{py.relative_to(REPO)}:{i + 1}", res.group(1), prel.group(1)))
    return found


def test_every_parent_edge_relation_exists_on_the_child_type() -> None:
    """The `annotation_project#tenant` class of bug, made mechanical."""
    model = _model_relations()
    phantom = [f"{loc} -> {res}#{rel}" for loc, res, rel in _parent_edge_literals() if res in model and rel not in model[res]]
    assert not phantom, (
        "a `grant_on_create` parent edge names a relation that does not exist on the child type: "
        f"{phantom}. The write SUCCEEDS and the inheritance silently does not exist — every rung on "
        "the object then denies for everyone, creator included."
    )


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
    # Side-loaded images: see `rask.image` in _helpers.tpl — the chart refuses a bare
    # `<component>:<tag>` unless this is set, because that is docker.io and not a local image.
    argv += ["--set", "image.localImages=true"]
    # The identity values every render needs since auth defaults ON (2026-08-06). The chart refuses
    # OIDC without a session secret ON PURPOSE — that refusal is what stops a forgotten values file
    # installing an ungoverned estate, and it is asserted directly by
    # `test_the_chart_REFUSES_to_render_oidc_without_a_session_secret`. Supplying dev values HERE
    # keeps every other render test testing its own subject rather than re-testing the guard.
    argv += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
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
        "notifications",
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
    ("services/maintenance/src/maintenance/core/config.py", 'default="lineage.events.v1", alias="MAINTENANCE_LINEAGE_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="lineage.events.v1", alias="MEDALLION_LINEAGE_TOPIC"'),
    # The medallion's publication head rides the CATALOG's control topic, so it IMPORTS the producer's
    # constant rather than re-typing the name — what catalog + maintenance already do. Pinned as the
    # import AND the use: either alone can be satisfied while the other re-literalizes the string.
    ("services/medallion/src/medallion/core/config.py", "from service_kit.control_events import CONTROL_TOPIC"),
    ("services/medallion/src/medallion/core/config.py", 'default=CONTROL_TOPIC, alias="MEDALLION_CONTROL_TOPIC"'),
    # the intra-cascade trigger topics (unversioned by design: both ends deploy atomically from one chart)
    ("services/medallion/src/medallion/core/config.py", 'default="medallion.bronze", alias="MEDALLION_SUB_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="medallion.bronze", alias="MEDALLION_BRONZE_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="training.jobs", alias="MEDALLION_TRAIN_TOPIC"'),
    ("services/medallion/src/medallion/core/config.py", 'default="medallion.media", alias="MEDALLION_MEDIA_TOPIC"'),
    # The inbox is the SECOND consumer of the run-lifecycle topic, and the one that reads it as a
    # notification rather than as graph input — so the name it subscribes is the same compatibility
    # unit, pinned in the same act.
    ("services/notifications/src/notifications/api/settings.py", 'default="lineage.events.v1", alias="RASK_NOTIFICATIONS_LINEAGE_TOPIC"'),
    # the stream bindings the topics land on (nats-stream-job) + the DLQ parking subjects
    ("chart/templates/nats-stream-job.yaml", 'add_if_missing CATALOG_CONTROL "catalog.control.>"'),
    ("chart/templates/nats-stream-job.yaml", 'add_if_missing DLQ "dlq.>"'),
    ("chart/templates/services.yaml", 'LINEAGE_DLQ_TOPIC, value: "dlq.lineage.events"'),
    # Per-app DLQ subject, never a shared one — §2.11's two-apps-counting-each-other's-parks defect.
    # Rendered by the configmap under the resiliency gate, because the app ships the setting empty:
    # dead-lettering without a retry policy behind it parks on the first failure.
    ("chart/templates/configmap.yaml", 'RASK_NOTIFICATIONS_DLQ_TOPIC: "dlq.notifications"'),
]


@pytest.mark.parametrize(("relpath", "needle"), _PINNED_TOPICS, ids=[n for _, n in _PINNED_TOPICS])
def test_event_topic_constants_are_pinned(relpath: str, needle: str) -> None:
    """DATA-CONTRACT §7.2 names these exact topics; this pin keeps the doc and the code from drifting."""
    assert needle in (REPO / relpath).read_text(), (
        f"{relpath} no longer contains `{needle}` — the event-fabric topic contract (DATA-CONTRACT §7.2) "
        "names this exact constant. A deliberate rename must update the doc + this pin together; a "
        "BREAKING payload change must instead add a NEW .vN topic with parallel consumers."
    )


def test_medallion_never_re_types_the_catalog_control_topic() -> None:
    """The pin above says the constant is imported; this says no SECOND spelling of it may reappear.

    A pin is satisfiable by an import that some other module then shadows with its own literal — which
    is exactly how `core/config.py` came to carry `"catalog.control.v1"` while three other consumers
    imported `CONTROL_TOPIC`. The name belongs to its producer's model module (DATA-CONTRACT §7.2:
    "the ONE shared constant both sides import"); a rename there must reach every subscriber, and a
    duplicate is a subscriber the rename silently leaves listening to a topic nobody publishes to.
    """
    import ast  # local: the AST is the point — a prose mention of the topic in a comment is not a duplicate

    offenders = [
        f"{py.relative_to(REPO)}:{node.lineno}"
        for py in _svc("medallion").rglob("*.py")
        for node in ast.walk(ast.parse(py.read_text()))
        if isinstance(node, ast.Constant) and node.value == "catalog.control.v1"
    ]
    assert not offenders, (
        f"{offenders} spell `catalog.control.v1` out as a string literal instead of importing CONTROL_TOPIC "
        "from service_kit.control_events — the topic name has exactly one definition site (DATA-CONTRACT §7.2)."
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


# --------------------------------------------------------------------------------------------------
# 9. The live-proof 2026-07-28 defect classes (docs/architecture/live-proof-2026-07-28.md)
#
# A first real end-to-end install on a stock kind cluster needed SIX manual overrides and one
# `kubectl set env` before it would come up. Every one of them was a chart default that only works on
# the maintainer's k3s box, and every failure mode was SILENT (Pending PVCs, ContainerCreating
# DaemonSets, a RayService stuck Initializing). Prose in a values comment cannot catch those; a render
# can. These guards are what make the fixes stay fixed.
# --------------------------------------------------------------------------------------------------


def _helm_notes(*set_values: str) -> str:
    """The rendered NOTES.txt — `helm template` deliberately omits it, and `helm install --dry-run`
    needs a live cluster (and trips over CRD ownership from an existing release), so neither can guard
    what the release notes actually SAY.

    A NOTES warning is the only thing some operators ever read, so its conditions deserve a real render
    rather than a grep over the template source. This builds a throwaway probe chart — the real
    values.yaml, the real _helpers.tpl, and the real NOTES.txt wrapped in a define + emitted as a
    ConfigMap value — so helm's own engine evaluates the same conditions with the same values.
    """
    import shutil
    import subprocess
    import tempfile

    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "notes-probe"
        (probe / "templates").mkdir(parents=True)
        (probe / "Chart.yaml").write_text("apiVersion: v2\nname: rask\nversion: 0.0.0\n")
        shutil.copy(CHART / "values.yaml", probe / "values.yaml")
        shutil.copy(CHART / "templates" / "_helpers.tpl", probe / "templates" / "_helpers.tpl")
        body = (CHART / "templates" / "NOTES.txt").read_text()
        (probe / "templates" / "notes-probe.yaml").write_text(
            '{{- define "notes.body" -}}\n'
            + body
            + "\n{{- end -}}\napiVersion: v1\nkind: ConfigMap\nmetadata: { name: notes-probe }\n"
            + 'data:\n  notes: {{ include "notes.body" . | quote }}\n'
        )
        argv = [helm, "template", "rask", str(probe)]
        # Side-loaded images: see `rask.image` in _helpers.tpl — the chart refuses a bare
        # `<component>:<tag>` unless this is set, because that is docker.io and not a local image.
        argv += ["--set", "image.localImages=true"]
        for value in set_values:
            argv += ["--set", value]
        out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603
    # The notes ride as one quoted scalar; unescape so assertions read naturally.
    return out.encode().decode("unicode_escape")


def _pvc_docs(rendered: str) -> list[str]:
    return [d for d in rendered.split("\n---") if re.search(r"^kind: PersistentVolumeClaim$", d, re.MULTILINE)]


def test_no_pvc_hardcodes_a_provisioner_specific_storage_class() -> None:
    """`storageClassName: local-path` is k3s's provisioner NAME, and it does not exist anywhere else.

    kind's default class is `standard`; a managed cluster's is `gp3`/`standard-rwo`/whatever. A PVC that
    names a class the cluster does not have sits **Pending forever** — no pod event, no chart complaint,
    and (for the RustFS Tenant and the Ray HF cache) it takes the whole data plane or the Ray head down
    with it. That is exactly how live-proof defect 2 burned an install.

    The portable answer is to OMIT `storageClassName` so the cluster's DEFAULT class provisions, which is
    why the chart reads its class from a value that defaults to `""` and renders the key only `with` a
    non-empty one. This asserts on the RENDER: no PVC the chart emits may carry a hardcoded class, and no
    template may name one as a literal. (Note `storageClassName: ""` is NOT the same as omitting it — the
    empty string DISABLES dynamic provisioning — so an empty literal is an offender too.)
    """
    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true", "explorer.corpus.mode=pvc")
    assert _pvc_docs(rendered), "the chart rendered no PVCs — this guard would pass vacuously"
    offenders: list[str] = []
    # EVERY doc, not just kind: PersistentVolumeClaim — the four RustFS Tenant volumes are a
    # `volumeClaimTemplate` INSIDE the Tenant CR, which is precisely where defect 2 lived, and a
    # PVC-kind-only scan would have declared the fix verified while missing it. CRDs are skipped:
    # their OpenAPI schemas describe `storageClassName` as documentation, not as a value.
    for doc in rendered.split("\n---"):
        if re.search(r"^kind: CustomResourceDefinition$", doc, re.MULTILINE):
            continue
        name = re.search(r"^  name: (\S+)", doc, re.MULTILINE)
        kind = re.search(r"^kind: (\S+)", doc, re.MULTILINE)
        for cls in re.finditer(r"^\s+storageClassName:\s*(\S.*)$", doc, re.MULTILINE):
            offenders.append(f"{kind.group(1) if kind else '?'}/{name.group(1) if name else '?'} pins storageClassName: {cls.group(1)}")
    assert not offenders, (
        "these rendered PVCs pin a StorageClass with the chart's DEFAULT values, so the install only works "
        "on a cluster that happens to have that class:\n  " + "\n  ".join(offenders)
    )

    literals = [
        f"{path.relative_to(REPO)}:{n}: {line.strip()}"
        for path in sorted((CHART / "templates").rglob("*"))
        if path.is_file()
        for n, line in enumerate(_uncommented(path.read_text()).splitlines(), 1)
        if re.search(r"storageClassName:\s*[\"']?[a-zA-Z]", line)
    ]
    assert not literals, "a StorageClass name must come from values (empty => the cluster default), never a template literal:\n  " + "\n  ".join(literals)


def test_a_gpuless_estate_renders_a_gpuless_ray_serve() -> None:
    """ray.gpuCount=0 must make the htrflow Serve actor ask for 0 GPU — or nothing ever becomes ready.

    live-proof defect 3: `config.RASK_SERVE_GPU_FRAC` defaulted to "1.0" INDEPENDENTLY of ray.gpuCount, so
    a GPU-less cluster deployed a Serve deployment waiting on a resource that would never be advertised.
    The RayService then stayed `Initializing` forever, KubeRay never created the stable
    `<release>-ray-head-svc`, and the compute zone reported "Ray offline" beside a Running, healthy head —
    three symptoms, none of them pointing at the actual cause.

    Both render sites are checked, because a fraction that is right in the RayService and wrong in the
    fleet ConfigMap is the next debugging round.
    """
    rendered = _helm_template("singleTenant.enabled=true")  # ray.gpuCount defaults to 0
    fracs = re.findall(r"RASK_SERVE_GPU_FRAC:\s*\"?([0-9.]+)\"?", rendered)
    assert fracs, "RASK_SERVE_GPU_FRAC renders nowhere — the guard would pass vacuously"
    assert all(float(f) == 0.0 for f in fracs), (
        f"with ray.gpuCount=0 every RASK_SERVE_GPU_FRAC must derive to 0, got {fracs} — a Serve actor that "
        "reserves a GPU on a GPU-less cluster leaves the RayService Initializing forever"
    )
    rayservice = next((d for d in rendered.split("\n---") if re.search(r"^kind: RayService$", d, re.MULTILINE)), None)
    assert rayservice is not None, "singleTenant.enabled=true rendered no RayService"
    assert "nvidia.com/gpu" not in rayservice, "a GPU-less head must not request the nvidia.com/gpu extended resource"
    assert "runtimeClassName" not in rayservice, (
        "a GPU-less head must not name the nvidia RuntimeClass — its handler is not registered on a CPU node "
        "(and runtimeclass.yaml does not even create the object), so every head pod fails to create"
    )

    # ...and the GPU posture still renders the GPU wiring, or the derivation is a one-way ratchet.
    gpu = _helm_template("singleTenant.enabled=true", "ray.gpuCount=1", "nvdp.enabled=true")
    gpu_service = next((d for d in gpu.split("\n---") if re.search(r"^kind: RayService$", d, re.MULTILINE)), None)
    assert gpu_service is not None
    assert re.search(r"RASK_SERVE_GPU_FRAC:\s*\"1.0\"", gpu_service), "ray.gpuCount=1 must pass config.RASK_SERVE_GPU_FRAC through"
    assert "nvidia.com/gpu: 1" in gpu_service and "runtimeClassName: nvidia" in gpu_service
    assert re.search(r"^kind: RuntimeClass$", gpu, re.MULTILINE), "a GPU estate must create the nvidia RuntimeClass"


def test_the_gpu_device_plugin_cannot_render_without_gpu_workloads() -> None:
    """nvdp + GPU-Feature-Discovery on a GPU-less cluster = two DaemonSets ContainerCreating forever.

    live-proof defect 6. Helm resolves a subchart `condition:` against a STATIC values path and cannot
    express `ray.gpuCount > 0`, so the chart cannot gate the device plugin on the same expression it gates
    every other GPU render on. It fails the render on the incoherent pair instead — which is a gate, just
    one that speaks. The reverse pair is legitimate (an externally-managed device plugin) and only warns.
    """
    import subprocess

    with pytest.raises(subprocess.CalledProcessError) as exc:
        _helm_template("nvdp.enabled=true")  # ray.gpuCount defaults to 0
    message = (exc.value.stderr or "") + (exc.value.stdout or "")
    assert "nvdp.enabled=true" in message and "ray.gpuCount" in message, f"the render must fail with the incoherent pair NAMED, got: {message[:400]}"
    assert "--set nvdp.enabled=false" in message, "a fail-closed guard must carry the fix in the message"

    # The coherent GPU pair renders, and it renders the plugin.
    gpu = _helm_template("ray.gpuCount=1", "nvdp.enabled=true")
    assert "nvdp" in gpu, "the coherent GPU pair must actually install the device plugin"
    # The default (GPU-less) render must carry neither DaemonSet.
    plain = _helm_template()
    daemonsets = [d for d in plain.split("\n---") if re.search(r"^kind: DaemonSet$", d, re.MULTILINE)]
    gpu_daemons = [d for d in daemonsets if "nvdp" in d or "gpu-feature-discovery" in d]
    assert not gpu_daemons, (
        f"the DEFAULT (GPU-less) render still emits {len(gpu_daemons)} GPU DaemonSet(s) — they will sit "
        "ContainerCreating forever on any cluster without an nvidia OCI runtime"
    )


def test_no_workload_mounts_a_hostpath_that_must_pre_exist() -> None:
    """`hostPath` + `type: Directory` = ContainerCreating forever unless a human prepared the node.

    live-proof defect 5: `explorer.enabled=true` mounted /var/media-corpus with `type: Directory`, so on a
    fresh cluster all three media pods wedged with a kubelet event as the only clue — and the plan already
    said no hostPath ships. First-party workloads must default to a volume that needs no node preparation
    (emptyDir for dev, a PVC for prod); where a node-local path IS the point (the OTel collector tailing
    /var/log/pods) it must be `DirectoryOrCreate`, never the fail-if-absent `Directory`.
    """
    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true", "observability.enabled=true")
    offenders: list[str] = []
    for doc in rendered.split("\n---"):
        if "hostPath" not in doc:
            continue
        name = re.search(r"^  name: (\S+)", doc, re.MULTILINE)
        for m in re.finditer(r"hostPath:\s*\{?[^}\n]*", doc):
            if re.search(r"type:\s*Directory\b(?!OrCreate)", m.group(0)):
                offenders.append(f"{name.group(1) if name else '?'}: {m.group(0).strip()[:90]}")
    assert not offenders, (
        "these rendered workloads mount a hostPath that must ALREADY exist on the node, so a fresh cluster "
        "wedges them in ContainerCreating:\n  " + "\n  ".join(offenders)
    )
    # The media corpus specifically: the DEFAULT must be node-independent.
    media_pods = [d for d in rendered.split("\n---") if "app.kubernetes.io/component: viewer" in d and "kind: Deployment" in d]
    assert media_pods, "explorer.enabled=true rendered no viewer Deployment"
    assert "hostPath" not in media_pods[0], (
        "the media corpus volume still defaults to a hostPath — set explorer.corpus.mode's default to a volume that works on an unprepared node"
    )


def test_the_lineage_durability_chain_is_on_by_default() -> None:
    """The #4 chain (stage -> publish -> drain) shipped OFF while NOTES warned about it on every install.

    live-proof defect 7. A default that the release notes tell every operator to change is the wrong
    default; worse, the warning became scenery. Both halves are ON now — and the pair matters: reconcile
    without the outbox can only back-fill that a version exists (author/inputs/columnLineage are gone),
    and the outbox without reconcile stages events nothing drains. This pins BOTH, and pins that the NOTES
    warning still fires (loudly) when someone turns one off.
    """
    import yaml

    values = yaml.safe_load((CHART / "values.yaml").read_text())
    lineage = values["services"]["lineage"]
    assert lineage["reconcile"]["enabled"] is True, "services.lineage.reconcile must default ON (it drains the outbox)"
    assert lineage["outbox"]["enabled"] is True, "services.lineage.outbox must default ON (reconcile has nothing to drain without it)"

    assert "DATA LOSS WINDOW OPEN" not in _helm_notes(), "the default install must not print a durability warning it cannot act on"
    broken = _helm_notes("services.lineage.reconcile.enabled=false")
    assert "DATA LOSS WINDOW OPEN" in broken and "reconcile.enabled=false" in broken, (
        f"disabling reconcile must produce an unmissable NOTES warning naming the half that is missing; got:\n{broken}"
    )
    half = _helm_notes("services.lineage.outbox.enabled=false")
    assert "outbox.enabled=false" in half and "PERMANENTLY" in half, (
        f"disabling only the outbox must say what is lost (author/inputs/columnLineage), got:\n{half}"
    )


# --------------------------------------------------------------------------------------------------
# 10. The 2026-07-28 install-flow defects (live-proof "Install-flow notes" + defects 1 and 3)
# --------------------------------------------------------------------------------------------------


def _docs(rendered: str) -> list[str]:
    return rendered.split("\n---")


def _job_by_component(rendered: str, component: str) -> str | None:
    """The rendered Job carrying `app.kubernetes.io/component: <component>`.

    Selected by LABEL, not by name: `_helm_template` renders without a release name (helm's
    `release-name` placeholder), and the bootstrap Jobs now carry a release-revision suffix — a
    name-prefix match would encode both accidents.
    """
    for doc in _docs(rendered):
        if not re.search(r"^kind: Job$", doc, re.MULTILINE):
            continue
        if re.search(rf"^\s+app\.kubernetes\.io/component: {re.escape(component)}$", doc, re.MULTILINE):
            return doc
    return None


#: The bootstrap Jobs that other release resources must wait for IN ORDER TO BECOME READY: the OpenFGA
#: schema migration (the server crash-loops against an unmigrated datastore), the OpenBao seed (the
#: medallion movers resolve S3 creds through the Dapr secret store at boot), the JetStream provisioner
#: (a daprd sidecar subscribes at startup) and the bucket-init (the lakehouse apps' object store).
_BOOTSTRAP_JOBS = ["openfga-migrate", "openbao-seed", "nats-stream", "rustfs-mkbucket"]

#: The inverse set — Jobs that wait for the APPS and that nothing waits on. A post-install hook is
#: exactly the right shape for these, and converting them would be the same mistake mirrored.
_POST_APP_HOOKS = ["greptimedb-ttl", "kueue-setup"]


def test_no_job_that_gates_readiness_is_a_post_install_hook() -> None:
    """`helm install --wait` must be safe on a fresh cluster, with no wrapper script.

    live-proof defect 1. helm's order is: pre-install hooks -> apply manifests -> (--wait) block until
    every resource is Ready -> post-install hooks. The OpenFGA schema migration was a POST-install hook
    and the OpenFGA server cannot start against an unmigrated datastore, so `--wait` blocked on the
    server, the server blocked on the migration, and the migration was queued behind `--wait`. Revs 1
    and 2 of the live-proof install died on "context deadline exceeded"; the OpenBao seed (also a
    post-install hook) never fired at all; and `scripts/e2e_stack.sh` exists in part to drop `--wait`
    and re-sequence the whole thing by hand.

    Moving those hooks EARLIER cannot fix it either: a `pre-install` hook runs before ANY release
    manifest exists, so the migrate Job would wait for an AGE Postgres that has not been created yet —
    the same deadlock one phase to the left, now blocking every other resource too. The fix is to take
    them out of the hook lifecycle entirely, which is what this pins.
    """
    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true")
    offenders: list[str] = []
    for comp in _BOOTSTRAP_JOBS:
        doc = _job_by_component(rendered, comp)
        assert doc is not None, f"{comp} renders no Job with the default values — this guard would pass vacuously"
        if re.search(r'^\s*"?helm\.sh/hook"?:', doc, re.MULTILINE):
            offenders.append(comp)
    assert not offenders, (
        "these Jobs gate another resource's READINESS, so as helm hooks they deadlock `helm install --wait`: "
        + ", ".join(offenders)
        + " — apply them in the ordinary wave (see 'BOOTSTRAP JOBS' in chart/templates/_helpers.tpl)"
    )

    # A plain Job's spec is immutable, so the name must change per revision or `helm upgrade` fails with
    # "field is immutable" the first time anything about the Job changes.
    for comp in _BOOTSTRAP_JOBS:
        doc = _job_by_component(rendered, comp)
        assert doc is not None
        name = re.search(r"^  name: (\S+)", doc, re.MULTILINE)
        assert name is not None and re.search(r"-r\d+$", name.group(1)), (
            f"{comp} must carry the release revision in its name (a plain Job's spec is immutable), got {name and name.group(1)}"
        )

    # The mirror image: a Job that waits for the APPS must NOT be pulled into the wave, or `--wait`
    # starts blocking on something that by definition cannot finish until the wait is over.
    for comp in _POST_APP_HOOKS:
        doc = _job_by_component(rendered, comp)
        assert doc is not None, f"{comp} renders no Job — this half of the guard would pass vacuously"
        assert re.search(r'^\s*"?helm\.sh/hook"?:\s*post-install', doc, re.MULTILINE), (
            f"{comp} waits for the apps and nothing waits on it — it belongs in post-install, not the apply wave"
        )


def test_the_bucket_init_verifies_the_buckets_the_operator_owns() -> None:
    """A provisioner that never checks what it did NOT provision is how a data plane goes missing.

    live-proof defect 3: the rustfs-operator refused to reconcile the Tenant
    (StatefulSetUpdateValidationFailed, "Reconcile is blocked by user-fixable configuration"), so
    `spec.buckets` — the whole static platform set — was never created. The bucket-init Job went green
    because it only ever created its OWN values-driven buckets, every layer in between reported
    healthy, and the single symptom was an HTTP 500 in the storage browser three services away.

    So the Job must VERIFY the operator-owned set and fail loudly, with the diagnosis in the message.
    It must NOT create them: `mc mb` here would paper over a Tenant that is still broken for
    everything else it owns.
    """
    import yaml

    values = yaml.safe_load((CHART / "values.yaml").read_text())
    expected = values["rustfs"]["buckets"]
    assert expected, "rustfs.buckets is empty — this guard would pass vacuously"

    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true")
    job = _job_by_component(rendered, "rustfs-mkbucket")
    assert job is not None, "the bucket-init Job does not render"

    missing = [b for b in expected if b not in job]
    assert not missing, f"the bucket-init Job never mentions these operator-owned buckets, so it cannot notice they are absent: {missing}"

    assert "exit 1" in job, "the bucket-init Job must FAIL when the operator-owned buckets are absent, not log and pass"
    assert "kubectl describe tenant" in job, "the failure message must carry the command that shows WHY the Tenant did not reconcile"
    assert "rustfs.storageClass" in job, "the failure message must name the usual cause (a StorageClass the cluster does not have)"
    # The create/verify split: the operator's buckets must not be silently created behind its back.
    for bucket in expected:
        assert f"mc mb --ignore-existing rfs/{bucket}\n" not in job or bucket == values["rustfs"]["bucket"], (
            f"{bucket} is operator-owned (rustfs.buckets) — creating it here would mask a Tenant that never reconciled"
        )


def test_every_dapr_annotated_pod_carries_the_injector_webhook_label() -> None:
    """Fail-closed sidecar injection is only safe if the two markers stay in lockstep.

    The second install-time ordering defect of 2026-07-28: Dapr's injector is a mutating webhook
    shipping `failurePolicy: Ignore`, and the Dapr control plane is a SUBCHART of this release — so on
    a fresh cluster the app pods are created alongside the injector, the API server calls a webhook
    with no endpoints, and it SILENTLY admits them with no daprd container. Nothing recreates such a
    pod (a CrashLoopBackOff restarts the container inside the same pod), so every governed app dies
    forever on "secret unavailable from Dapr store … failing closed" and `helm install --wait` times
    out. `scripts/e2e_stack.sh` works around it by deleting and recreating the app pods by hand.

    The chart now sets `failurePolicy: Fail` with an `objectSelector` on the `dapr.io/enabled` LABEL.
    That makes the correspondence load-bearing in BOTH directions of failure:
      * annotation without label -> the webhook is never called -> silently un-injected again;
      * an unscoped `Fail` -> the injector's own pod cannot be admitted -> the cluster wedges.
    So: every pod template carrying the annotation must carry the label, and the webhook's selector
    must be exactly that label.
    """
    import yaml

    rendered = _helm_template("singleTenant.enabled=true", "explorer.enabled=true")
    docs = [d for d in yaml.safe_load_all(rendered) if d]

    webhook = next(
        (w for d in docs if d.get("kind") == "MutatingWebhookConfiguration" for w in (d.get("webhooks") or []) if w.get("name") == "sidecar-injector.dapr.io"),
        None,
    )
    assert webhook is not None, "the Dapr sidecar-injector webhook does not render — this guard would pass vacuously"
    assert webhook["failurePolicy"] == "Fail", (
        "the Dapr injector webhook must fail CLOSED: with `Ignore` a pod created before the injector is "
        "ready is admitted with no sidecar, and nothing ever fixes it"
    )
    selector = (webhook.get("objectSelector") or {}).get("matchLabels") or {}
    assert selector == {"dapr.io/enabled": "true"}, (
        f"a fail-closed POD webhook MUST be scoped by objectSelector or it blocks its own injector pod and wedges the cluster; got {selector!r}"
    )

    annotated = 0
    missing: list[str] = []
    for doc in docs:
        template = (doc.get("spec") or {}).get("template") if isinstance(doc.get("spec"), dict) else None
        if not isinstance(template, dict):
            continue
        meta = template.get("metadata") or {}
        if (meta.get("annotations") or {}).get("dapr.io/enabled") != "true":
            continue
        annotated += 1
        if (meta.get("labels") or {}).get("dapr.io/enabled") != "true":
            missing.append(f"{doc.get('kind')}/{(doc.get('metadata') or {}).get('name')}")
    assert annotated, "no pod template asks for a Dapr sidecar — this guard would pass vacuously"
    assert not missing, (
        "these pod templates want a sidecar (dapr.io/enabled ANNOTATION) but do not carry the matching "
        f"LABEL, so the fail-closed webhook skips them and they come up un-injected: {missing}"
    )


def test_every_pod_whose_app_fails_closed_on_the_app_token_is_given_one() -> None:
    """A pod running code that REFUSES TO START without APP_API_TOKEN must be rendered one.

    `service_kit.governed.dapr_auth.assert_app_token_configured` raises at startup when Dapr ingest is
    on and the token is unset — deliberately, because the alternative is a live sidecar-delivered route
    with no authentication. That makes the token a startup PRECONDITION for those apps, and a
    precondition the chart can omit silently: the render is valid YAML, `helm upgrade` succeeds, and the
    pod CrashLoopBackOffs with the reason buried in container logs.

    `notifications` shipped exactly that. It writes no Lance, and in fleet.yaml the token had only ever
    been reachable from inside `if $svc.lanceWriter` — an unrelated STORAGE flag — so the first fleet
    service to host a Dapr-delivered route rendered `RASK_DAPR_ENABLED=true` with no token at all. Every
    prior caller of the assert is a lance-plane pod templated somewhere else, each naming APP_API_TOKEN
    explicitly, which is why one shared render site had never been needed and its absence never showed.

    Derived from the module each Deployment actually RUNS, not from a hand-kept list of service names —
    a list is the thing that drifts, which is the lesson the lineage-allowlist gate already encodes.
    """
    rendered = _helm_template()

    fail_closed = {path.relative_to(SERVICES).parts[0] for path in SERVICES.rglob("*.py") if "assert_app_token_configured(" in path.read_text(errors="ignore")}
    assert fail_closed, "no service calls assert_app_token_configured — this guard has nothing to check"

    # fleet.yaml renders `args: - "<svc>:app"`; the lance templates render `<pkg>.<module>:app`. Both
    # name the import root, which is the services/ directory name.
    module_re = re.compile(r'^\s*-\s+"?([a-z_]+)(?:\.[a-z_.]+)?:app"?\s*$', re.MULTILINE)
    checked = 0
    missing: list[str] = []
    for doc in yaml.safe_load_all(rendered):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        raw = yaml.safe_dump(doc)
        if not {m.group(1) for m in module_re.finditer(raw)} & fail_closed:
            continue
        checked += 1
        if "APP_API_TOKEN" not in raw:
            missing.append((doc.get("metadata") or {}).get("name", "?"))

    assert checked, "no rendered Deployment runs a fail-closed module — the module parse has drifted"
    assert not missing, (
        f"{sorted(missing)} run code that calls assert_app_token_configured but are rendered WITHOUT "
        "APP_API_TOKEN. The chart installs cleanly and the pod CrashLoopBackOffs on startup. Give the "
        "service `daprIngest: true` in values.yaml (fleet.yaml) or render the token in its own template."
    )


def test_the_ingress_admits_a_real_page_image() -> None:
    """ingress-nginx caps a request body at **1 MB** by default, and every image surface in this
    estate is larger than that.

    Measured 2026-08-06 on the deployed stack: the inference playground refused a 2239 kB page image
    with `HTTP 413`. The zone's own guard allows 25 MB and the Ray Serve ingress never saw the bytes
    — the EDGE rejected them, so the failure read as "inference is broken" rather than "the ingress
    has a 1 MB cap". The same cap sits in front of the annotator's Arrow import and the medallion's
    multi-image batch posts.

    Asserted RENDERED and numerically, exactly like the read-timeout sibling above: an annotation
    that is merely present but smaller than a real page image is decoration.
    """
    rendered = _helm_template("ingress.enabled=true")
    ingress = next((doc for doc in rendered.split("\n---") if re.search(r"^kind: Ingress$", doc, re.MULTILINE)), None)
    assert ingress is not None, "ingress.enabled=true rendered no Ingress"
    m = re.search(r"nginx\.ingress\.kubernetes\.io/proxy-body-size:\s*\"?(\d+)([mMgG])\"?", ingress)
    assert m, "the Ingress carries no nginx.ingress.kubernetes.io/proxy-body-size — nginx's 1 MB default 413s every page image before it reaches a zone"
    megabytes = int(m.group(1)) * (1024 if m.group(2).lower() == "g" else 1)
    assert megabytes >= 25, f"proxy-body-size is {m.group(0)} — below the zones' own 25 MB inference guard, so the edge is the tighter limit"


def test_auth_is_ON_by_default_and_an_open_estate_must_be_ASKED_for() -> None:
    """A security default has to fail CLOSED.

    `auth.enabled` and `frontend.oidc.enabled` were both `false` in values.yaml, with
    values-prod.yaml flipping them — so every install that forgot `-f values-prod.yaml` came up
    UNGOVERNED, green, and indistinguishable from a governed one. Measured 2026-08-06: helm revision
    29 landed exactly that way, which also stopped rendering the session Secret and left
    `rask-web-models` unable to start a pod against a spec that still referenced it.

    Both must be ON in the base values, and they must move TOGETHER — the chart refuses the
    half-governed pair (templates/auth-consistency.yaml), so a default that enabled one and not the
    other would fail every render instead of protecting anything.
    """
    values = yaml.safe_load((REPO / "chart/values.yaml").read_text())
    assert values["auth"]["enabled"] is True, "auth defaults OFF — a forgotten values file silently un-governs the estate"
    assert values["frontend"]["oidc"]["enabled"] is True, "the UI's sign-in flow defaults OFF while auth defaults ON — the chart refuses that pair"


def test_the_chart_REFUSES_to_render_oidc_without_a_session_secret() -> None:
    """The other half of failing closed: enabling the flow is not enough if the cookie is unsealed.

    Deliberately asserted as a RENDER FAILURE rather than a defaulted value. Shipping a plausible
    session secret in values.yaml is how a dev key reaches production — the local loop supplies its
    own in the Makefile, in the open, where it is obviously a dev value.
    """
    # subprocess directly: `_helm_template` raises on a non-zero exit, and the non-zero exit IS the
    # assertion here.
    import shutil
    import subprocess

    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    result = subprocess.run(
        [helm, "template", "rask", str(REPO / "chart"), "--set", "image.localImages=true"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode != 0, "a bare render succeeded — it must refuse without a sessionSecret"
    assert "sessionSecret" in result.stderr, f"it refused for the wrong reason: {result.stderr[-300:]}"


def test_a_per_component_pin_beats_the_global_tag_and_a_digest_beats_the_pin() -> None:
    """#135 — the chart must be able to describe a fleet that is NOT one tag.

    One `image.tag` could not: a live estate ran a catalog tag across 11 services, a different tag on
    gateway/compute/controlplane, a third across the 7 zones, and an ingest DIGEST. Rendering all of
    that from one value is what made `helm upgrade` destructive — every image rewritten to a tag the
    node did not hold, whole fleet ImagePullBackOff, recovered by hand. Observed twice on 2026-08-06.

    Precedence is asserted end to end, because a pin that is merely ACCEPTED and then overridden is
    worse than no pin: it reads as safety while the deploy still rewrites the image.
    """
    pinned = _helm_template("image.localImages=false", "image.repository=reg.example", "image.tags.gateway=PINNED")
    assert 'image: "reg.example/gateway:PINNED"' in pinned, "a per-component tag did not reach the render"
    assert 'image: "reg.example/compute:dev"' in pinned, "the pin leaked onto a component that did not ask for it"

    # A digest is a CONTENT pin — a reconciler's, typically — so no tag may undo it.
    both = _helm_template("image.localImages=false", "image.repository=reg.example", "image.tags.gateway=IGNORED", "image.digests.gateway=sha256:abc")
    assert 'image: "reg.example/gateway@sha256:abc"' in both, "a per-component tag overrode a digest"


@pytest.mark.parametrize("ray_enabled", ["true", "false"])
def test_no_env_var_is_rendered_TWICE_on_any_workload(ray_enabled: str) -> None:
    """A duplicated env NAME is not a cosmetic defect — it makes the release unupgradable.

    Kubernetes' strategic-merge-patch keys the env list by `name`, so a duplicate makes the patch's
    element order disagree with `$setElementOrder` and helm refuses with
    `The order in patch list … doesn't match`. On 2026-08-06 `COMPUTE_SERVE_URL` was rendered twice
    for the compute zone — once ungated, once in the ray-enabled block — and the resulting upgrade
    aborted AFTER partially applying, taking ~20 deployments to ImagePullBackOff.

    Parametrized over ray because that is precisely the toggle that produced the collision: the pair
    only overlapped when ray was on, so a single-mode check would have passed while the estate that
    matters broke.
    """
    rendered = _helm_template(f"ray.enabled={ray_enabled}")
    offenders: list[str] = []
    for doc in yaml.safe_load_all(rendered):
        if not doc or doc.get("kind") not in {"Deployment", "StatefulSet", "Job", "CronJob"}:
            continue
        spec = doc["spec"]["template"]["spec"] if doc["kind"] != "CronJob" else doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            names = [e["name"] for e in container.get("env", [])]
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                offenders.append(f"{doc['metadata']['name']}/{container['name']}: {sorted(dupes)}")
    assert offenders == [], f"duplicated env names make the release unupgradable: {offenders}"


def test_the_INGEST_stream_has_ONE_definition_and_the_chart_agrees_with_the_code() -> None:
    """Two definitions of one stream, reconciled only by "whoever creates it first wins".

    `services/ingest/.../queue.py` declares `RetentionPolicy.WORK_QUEUE` and BUILDS ON it — "a message
    is REMOVED once acked … which is why this plane needs no side ledger" is the reasoning that
    dissolved the tracker. The chart created the same stream with `--retention limits`, and in-cluster
    the Job wins. Measured on the live stream 2026-08-06: retention=limits, deny_purge=true,
    deny_delete=true, max_age=168h — so acked messages were RETAINED and `messages` was not
    outstanding work. The design claim was false exactly where it shipped.

    Three assertions because there were three statements and two were false:
      1. the chart's `--retention` for INGEST matches the code's RetentionPolicy;
      2. the chart does not contradict ITSELF (it described the stream as WorkQueuePolicy while
         creating it as limits, twenty-seven lines apart);
      3. `--deny-purge` / `--deny-delete` are absent on the work-queue path — they protect a retained
         RECORD, which a work queue does not have, and their only measured effect was making the
         plane's own `release_run -> purge_stream` return an inert 0 while the server refused it.
    """
    job = (REPO / "chart/templates/nats-stream-job.yaml").read_text()
    queue = (REPO / "services/ingest/src/ingest/queue.py").read_text()

    code_is_workqueue = "RetentionPolicy.WORK_QUEUE" in queue
    assert code_is_workqueue, "queue.py stopped declaring WORK_QUEUE — update this gate WITH the design change, not after it"

    # (1) The creation call INGEST actually uses.
    ingest_call = next((ln for ln in job.splitlines() if "INGEST" in ln and "_if_missing" in ln), "")
    assert ingest_call, "no creation call for INGEST — it is created somewhere this gate cannot see"
    fn = ingest_call.strip().split()[0]
    body = job.split(f"{fn}() {{", 1)[1].split("\n              }", 1)[0]
    assert "--retention work" in body, f"INGEST is created by {fn}() with a retention the code does not declare: {body.strip()[:200]}"

    # (3) …and without the flags that only make sense for a retained log.
    for flag in ("--deny-purge", "--deny-delete"):
        assert flag not in body, f"{flag} on a work queue protects a record it does not have, and breaks purge-based reclamation"

    # (2) The chart must not describe the stream as something it does not create.
    if "WorkQueuePolicy" in job:
        assert "--retention work" in body, "the chart calls INGEST a WorkQueuePolicy stream while creating it otherwise"


def test_the_lineage_allowlist_is_DERIVED_from_every_declared_identity() -> None:
    """The door's allowlist and the callers' claims must come from ONE value, not two lists.

    `LINEAGE_SERVICE_SUBJECTS` was a hand-written string naming three services, and a list that has
    to be kept in sync with the set of services that emit lineage WILL drift — nothing connected the
    two. It drifted twice: the trainer in 2026-07, and `service-ingest` on 2026-08-06, where every
    ingest emit 403'd for a day while the data landed perfectly.

    The failure is SILENT by construction, which is why a gate is the only thing that catches it. The
    emitter swallows a refused emit on purpose — a landed commit must not become a failed run — so
    the symptom is an absence in the graph, not an error anywhere.

    A service now DECLARES itself by setting `env.RASK_LINEAGE_SERVICE_IDENTITY`, and the chart reads
    those declarations. This asserts the derivation actually holds: declare an identity and you are
    admitted; the string cannot be edited out from under a declaration.
    """
    import re

    rendered = _helm_template("auth.enabled=true")

    declared = {m.group(1) for m in re.finditer(r'name:\s*RASK_LINEAGE_SERVICE_IDENTITY,\s*value:\s*"?([\w-]+)"?', rendered)}
    assert declared, "no service declares a lineage identity — the derivation has nothing to read, so this gate is vacuous"

    allowlist_match = re.search(r'name:\s*LINEAGE_SERVICE_SUBJECTS,\s*value:\s*"([^"]*)"', rendered)
    assert allowlist_match, "the lineage door renders no allowlist at all — every service emit will 401"
    allowed = set(allowlist_match.group(1).split(","))

    missing = declared - allowed
    assert not missing, (
        f"{sorted(missing)} declare RASK_LINEAGE_SERVICE_IDENTITY but are NOT in LINEAGE_SERVICE_SUBJECTS. "
        "Their lineage emits will 403 and the emitter will swallow it — the data lands and the graph "
        "stays empty, with nothing reporting the gap."
    )


def test_the_allowlist_admits_NO_EMPTY_subject() -> None:
    """An unset identity must contribute nothing, not an empty string.

    `compact` is what provides this. Without it a service whose identity is unset renders a bare `,,`
    and the door's allowlist contains "" — which admits a caller presenting no identity at all, on a
    door whose entire purpose is to require one.
    """
    import re

    rendered = _helm_template("auth.enabled=true")
    match = re.search(r'name:\s*LINEAGE_SERVICE_SUBJECTS,\s*value:\s*"([^"]*)"', rendered)
    assert match

    subjects = match.group(1).split(",")

    assert "" not in subjects, f"the allowlist contains an EMPTY subject: {subjects!r} — it would admit an unidentified caller"
    assert len(subjects) == len(set(subjects)), f"the allowlist repeats a subject: {subjects!r}"


def test_every_DURABLE_pubsub_component_has_a_sidecar_retry_target() -> None:
    """A trigger consumer that must not lose messages must also be named in the Resiliency CRD.

    THE REGRESSION THIS GATES. The cascade's control component
    (`catalog-control-pubsub-<producer>`) carried a `durableName` — the chart's own marker for "a
    trigger published while this app is down must be DELIVERED on recovery" — and was absent from
    the Resiliency CRD's `targets.components`, which ranges only over the `lance.subPubsub` family.
    The `/publication-arrival` subscription declares a `dead_letter_topic`, and Dapr sends there
    after the FIRST failure when no retry policy targets the component. So the most expensive
    trigger in the estate got zero retries and parked on one transient blip — while the CRD existed,
    the app was in `scopes:`, and every neighbour was covered.

    The rule is stated as a PROPERTY rather than a list of names on purpose: a future subscriber
    added to one template and not the other fails here, which is exactly how this one was missed.
    """
    rendered = _helm_template("dapr.enabled=true", "dapr.resiliency.enabled=true")
    docs = [d for d in yaml.safe_load_all(rendered) if d]

    durable = {
        d["metadata"]["name"]
        for d in docs
        if d.get("kind") == "Component"
        and (d.get("spec") or {}).get("type") == "pubsub.jetstream"
        and any(m.get("name") == "durableName" for m in (d.get("spec") or {}).get("metadata") or [])
        # DLQ components are EXEMPT, and not as a convenience. A dead-letter handler must
        # unconditionally ACK — a RETRY from a DLQ route requeues the message onto the DLQ forever —
        # so its subscription never returns RETRY and an inbound retry policy could never engage.
        # Requiring one would push a meaningless target into the CRD and teach the next reader that
        # a DLQ is retried, which is the opposite of the rule. (This exemption is not hypothetical:
        # `lineage-pubsub-lineage-dlq` is durable, by design, and correctly has no target.)
        and not d["metadata"]["name"].endswith("-dlq")
    }
    assert durable, "no durable pub/sub component rendered — this gate would pass vacuously"

    targeted: set[str] = set()
    for d in docs:
        if d.get("kind") != "Resiliency":
            continue
        targeted |= set(((d.get("spec") or {}).get("targets") or {}).get("components") or {})

    missing = durable - targeted
    assert not missing, (
        f"durable pub/sub components with NO inbound retry target: {sorted(missing)} — with a dead_letter_topic declared, Dapr parks these on the FIRST failure"
    )


def test_the_rustfs_tenant_carries_NO_plaintext_credential() -> None:
    """The Tenant CR's OIDC client secret must be a `secretKeyRef`, never a `value:`.

    THE REGRESSION THIS GATES. `RUSTFS_IDENTITY_OPENID_CLIENT_SECRET` shipped as
    `value: {{ .Values.dex.clientSecret }}` — readable in `kubectl get tenant -o yaml`,
    `kubectl describe` and `helm get manifest` — while every sibling credential in the estate was
    already behind a guard. It sat outside that guard because a Tenant is consumed by the RustFS
    operator, which has no daprd sidecar and so cannot read the Dapr secret store the fleet services
    use. That is the case `infra-credentials.yaml` exists for, and this asserts the Tenant actually
    uses it.

    Latent-by-default is not a defence: `rustfs.oidc.enabled` is off in the shipped values, so this
    renders only on estates running STS credential vending — which is precisely where a leaked client
    secret is worth the most.
    """
    rendered = _helm_template("rustfs.enabled=true", "rustfs.oidc.enabled=true")
    docs = [d for d in yaml.safe_load_all(rendered) if d]

    tenants = [d for d in docs if d.get("kind") == "Tenant"]
    assert tenants, "no Tenant rendered — this gate would pass vacuously"

    for tenant in tenants:
        for env in (tenant.get("spec") or {}).get("env") or []:
            if not str(env.get("name", "")).endswith("_CLIENT_SECRET"):
                continue
            assert "value" not in env, f"{env['name']} renders a PLAINTEXT value on the Tenant CR: {env!r}"
            ref = ((env.get("valueFrom") or {}).get("secretKeyRef")) or {}
            assert ref.get("name") and ref.get("key"), f"{env['name']} has neither a value nor a usable secretKeyRef: {env!r}"

            # The reference must RESOLVE — a secretKeyRef at an absent key is a pod that never starts,
            # and it would only surface on the enabled path, which is the narrowest possible place to
            # discover it.
            secrets = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Secret"}
            target = secrets.get(ref["name"])
            assert target is not None, f"{env['name']} references Secret {ref['name']!r}, which the chart does not render"
            keys = set(target.get("stringData") or {}) | set(target.get("data") or {})
            assert ref["key"] in keys, f"{env['name']} references key {ref['key']!r}, absent from Secret {ref['name']!r} (has {sorted(keys)})"


# --------------------------------------------------------------------------------------------------
# 12. COLLECTION — a suite that runs nowhere is a claim, not a gate
# --------------------------------------------------------------------------------------------------


def _configured_testpaths() -> list[str]:
    import tomllib

    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]["testpaths"]


def test_every_workspace_test_directory_is_in_the_root_testpaths() -> None:
    """`testpaths` is EXPLICIT, so writing a suite and running it are two separate acts.

    This estate has already paid for the gap three times over: `services/catalog/tests` and
    `services/lineage/tests` were written and collected by nothing, and three landed regression suites
    (the catalog's replayed-commit door, lineage's external-source authz and its privileged-identity
    door) sat green-by-absence until 2026-08-09. Every one of them passed review — the directory is not
    where anybody looks.

    Derived from the tree rather than from a list, so the day a new member lands with tests the gate
    already covers it. The reverse direction is checked too: a testpath naming a directory that no
    longer exists makes pytest print `file or directory not found` and exit 4, which fails the whole
    run rather than the one suite.
    """
    configured = set(_configured_testpaths())
    present = {str(path.relative_to(REPO)) for glob in ("packages/*/tests", "services/*/tests") for path in REPO.glob(glob) if path.is_dir()}
    assert present, "no workspace test directories found — this gate would pass vacuously"

    uncollected = sorted(path for path in present if path not in configured)
    assert not uncollected, (
        f"these test directories exist and are collected by NOTHING: {uncollected}. Every run stays green "
        "while the suites inside them never execute — add them to [tool.pytest.ini_options] testpaths."
    )

    stale = sorted(path for path in configured if path.startswith(("packages/", "services/")) and not (REPO / path).is_dir())
    assert not stale, f"these testpaths name a directory that does not exist — pytest exits 4 and NO suite runs: {stale}"


def test_the_notifications_suite_is_collected_by_name() -> None:
    """The generic gate above cannot notice a services/ layout change that stops the glob matching, and
    the notification plane is exactly the kind of new member whose suites nobody would miss for a
    while. Named, so this one cannot go quiet either way."""
    assert "services/notifications/tests" in _configured_testpaths()


def test_the_notifications_pod_asks_for_a_sidecar_and_is_allowed_to_receive_one() -> None:
    """The sidecar's TWO halves on the one Deployment that cannot work without it.

    `test_every_dapr_annotated_pod_carries_the_injector_webhook_label` proves the correspondence for
    every pod that asks — and says nothing at all about a pod that never asks. That is the vacuous case
    this closes: the whole notification plane is actor state plus one bus subscription, so a
    Deployment rendered with no `dapr.io/*` annotations would come up healthy, serve its health surface
    and its gateway row, and fail every inbox route forever — on a pod whose probes stay green,
    because actor registration is process-local and cannot notice that no sidecar was injected.

    The app-id is asserted because it is not decoration: `lance-statestore`'s `scopes`, the
    `lineage-pubsub-notifications` component's subscriber list and the resiliency CRD all key on that
    exact string, and a mismatch disables actor hosting with no error anywhere.
    """
    rendered = _helm_template()

    pod = next(
        (
            (doc.get("spec") or {}).get("template")
            for doc in yaml.safe_load_all(rendered)
            if isinstance(doc, dict) and doc.get("kind") == "Deployment" and "notifications" in ((doc.get("metadata") or {}).get("name") or "")
        ),
        None,
    )
    assert pod is not None, "no notifications Deployment rendered — the service is not deployed at all"

    meta = pod.get("metadata") or {}
    annotations = meta.get("annotations") or {}
    assert annotations.get("dapr.io/enabled") == "true", f"the notifications pod does not ask for a sidecar: {sorted(annotations)}"
    assert annotations.get("dapr.io/app-id") == "notifications"
    assert annotations.get("dapr.io/app-port") == "8850"
    assert (meta.get("labels") or {}).get("dapr.io/enabled") == "true", (
        "the notifications pod asks for a sidecar by ANNOTATION but carries no injector LABEL — the "
        "fail-closed webhook skips it and it comes up un-injected, with every inbox route failing"
    )


# --------------------------------------------------------------------------------------------------
# 12. The notification plane's WIRING — the four facts that live in the chart and nowhere else
#
# The service's own suites prove what each route answers; none of them can see whether anything ever
# reaches those routes. Each guard below covers one address that is written down in exactly one place,
# read somewhere else, and whose omission produces a HEALTHY pod: a gateway proxying to itself, a
# subscription on a component that does not exist, an actor host with no state store, and a kubelet
# probing a path the app does not serve. Three of the four have already shipped once in this estate on
# a neighbouring service.
# --------------------------------------------------------------------------------------------------


def _rendered_docs(*set_values: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(_helm_template(*set_values)) if isinstance(doc, dict)]


def _fleet_config(docs: list[dict]) -> dict[str, str]:
    """The fleet ConfigMap — the one carrying `RASK_API_PREFIX` and the gateway's upstream addresses."""
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and "RASK_API_PREFIX" in (doc.get("data") or {}):
            return doc["data"]
    raise AssertionError("no fleet ConfigMap rendered — every gateway upstream would fall back to a localhost default")


def _notifications_container(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "Deployment" and "notifications" in ((doc.get("metadata") or {}).get("name") or ""):
            return doc["spec"]["template"]["spec"]["containers"][0]
    raise AssertionError("no notifications Deployment rendered")


def test_the_gateway_learns_where_the_inbox_lives_rather_than_proxying_to_itself() -> None:
    """`RASK_NOTIFICATIONS_URL` is the only thing standing between the row and a self-proxy.

    The gateway's row defaults to `http://127.0.0.1:8850` — inside the gateway POD that address is the
    gateway, where no inbox route exists, so every call answers a 404 indistinguishable from an
    unrouted path while the service is up and healthy one Service name away. This is not a
    hypothetical: `RASK_INGEST_URL` shipped missing and the configmap now carries a comment saying so
    (`chart/templates/configmap.yaml`), which is a comment and not a gate.

    The port is compared against the container's OWN port rather than a literal: the failure this
    catches second is the address being right and the port stale.
    """
    docs = _rendered_docs()
    url = _fleet_config(docs).get("RASK_NOTIFICATIONS_URL")
    port = _notifications_container(docs)["ports"][0]["containerPort"]

    assert url, "the chart renders no RASK_NOTIFICATIONS_URL — the gateway's /api/notifications row proxies to the gateway itself"
    assert "127.0.0.1" not in url and "localhost" not in url, f"the gateway is pointed at itself: {url}"
    assert url.endswith(f":{port}"), f"the gateway addresses {url} while the pod listens on {port}"


def test_the_inbox_subscribes_on_a_component_the_chart_actually_renders() -> None:
    """A subscription names its pubsub component by string, and a name that resolves to nothing is a
    STARTUP error the pod survives: daprd logs it, the app serves its health surface and its gateway
    row, and not one lineage event is ever delivered.

    Both ends are asserted because they fail in different deployments. The chart's value is what the
    pod reads; the app's DEFAULT is what a dev run without the ConfigMap reads, and the two drifting
    apart is how a subscription works in one environment and is silently dead in the other. Same shape
    as `test_user_state_store_default_matches_the_component_the_catalog_is_scoped_to`, for the same
    reason: the name is a coordinate, and nothing else compares its two ends.
    """
    from notifications.api.settings import IngressSettings

    docs = _rendered_docs()
    configured = _fleet_config(docs)["RASK_NOTIFICATIONS_PUBSUB"]
    component = next((d for d in docs if d.get("kind") == "Component" and d["metadata"]["name"] == configured), None)

    assert component is not None, f"the inbox is configured to subscribe on {configured!r}, which the chart renders no Component for"
    assert IngressSettings.model_fields["pubsub"].default == configured, (
        f"the service defaults to {IngressSettings.model_fields['pubsub'].default!r} while the chart configures {configured!r} — "
        "a dev run and a deployed pod would subscribe on different components"
    )
    # `scopes` is a ROOT field of a Dapr Component, not part of `spec`: an unscoped app-id gets
    # "component not found" from its sidecar, which is the same silence as a missing component.
    assert "notifications" in (component.get("scopes") or []), f"{configured} is not scoped to notifications — its sidecar refuses to load it"


def test_the_inbox_subscription_starts_from_new_and_keeps_a_durable_cursor() -> None:
    """The two consumer settings that decide what a rollout does to a badge.

    `deliverPolicy: all` is right for lineage — replaying the retained stream into an idempotent MERGE
    is its outage-durability story — and catastrophic here: an inbox would re-notify a week of history
    on every deploy, which is a badge that lies loudest right after a release. `durableName` is the
    other half: without it the queue-group consumer is deleted with its last member, so a run that
    terminated while this app was down is skipped on reconnect and its author is never told.

    Both are chart-side facts with no code that can check them, and the pair is what makes the
    subscription correct — `new` alone loses events, durable alone replays them.
    """
    docs = _rendered_docs()
    configured = _fleet_config(docs)["RASK_NOTIFICATIONS_PUBSUB"]
    component = next(d for d in docs if d.get("kind") == "Component" and d["metadata"]["name"] == configured)
    settings = {m["name"]: m.get("value") for m in component["spec"]["metadata"]}

    assert settings.get("deliverPolicy") == "new", f"the inbox consumer is {settings.get('deliverPolicy')!r} — a rollout would re-notify the retained backlog"
    assert settings.get("durableName"), "the inbox consumer is ephemeral — a run that terminated while the pod was down never reaches its author"
    assert settings.get("queueGroupName") == "notifications", (
        "without its own queue group every replica receives every message (jetstream sets a DeliverGroup only when "
        "queueGroupName is non-empty), so a scaled deployment delivers each notification once per pod"
    )


def test_the_actor_state_store_is_scoped_to_the_service_whose_whole_state_it_is() -> None:
    """`lance-statestore` carries `actorStateStore: "true"`, and an app-id missing from its `scopes`
    gets "Actor state store not configured - actor hosting disabled" from its own sidecar.

    The notification plane is nothing BUT actor state — one InboxActor per subject holding the pointer
    rows and the compaction reminder — so unscoped it fails at every INVOCATION and at no earlier
    point: `ActorRuntime.register_actor` is process-local and cannot notice a missing scope, so
    `actors_registered` is True, `require_actor_plane` admits the request, and the sidecar's refusal
    reaches the caller untranslated. The pod is Ready with a permanently empty bell. Nothing crashes
    and nothing else would notice.

    Found by property rather than by name: the store is whichever Component declares itself the actor
    state store, so a rename cannot make this pass vacuously.
    """
    docs = _rendered_docs()
    stores = [
        doc
        for doc in docs
        if doc.get("kind") == "Component"
        and any(m.get("name") == "actorStateStore" and str(m.get("value")).lower() == "true" for m in (doc["spec"].get("metadata") or []))
    ]

    assert len(stores) == 1, f"expected exactly one actor state store, found {[d['metadata']['name'] for d in stores]}"
    assert "notifications" in (stores[0].get("scopes") or []), (
        f"{stores[0]['metadata']['name']} is not scoped to notifications — the sidecar disables actor hosting and every inbox route fails "
        "on a pod that reports itself healthy"
    )


def test_the_kubelet_probes_the_inbox_on_a_path_the_service_actually_serves() -> None:
    """The chart probes a LITERAL; the app derives that path from `RASK_API_PREFIX`. Nothing renders
    the two together, and a mismatch is a CrashLoopBackOff whose cause is in neither file.

    `chart/templates/fleet.yaml` takes `healthPath | default "/api/health"` for both probes, and
    `services.notifications` sets no `healthPath` — so the default is load-bearing here and is correct
    only while the prefix is `/api`. The service's own suite proves the badge is mounted UNDER the
    prefix (`services/notifications/tests/test_probe_wiring.py`); this is the other end of the same
    claim, and neither half can see the mismatch alone.

    The app is rebuilt under the chart's own prefix rather than the ambient one, because `app` is a
    module-level singleton built from the environment at import — asking the process's current app
    would answer about whichever suite imported it first.
    """
    import importlib
    import os

    docs = _rendered_docs()
    config = _fleet_config(docs)
    container = _notifications_container(docs)
    probed = {container[probe]["httpGet"]["path"] for probe in ("livenessProbe", "readinessProbe")}

    previous = os.environ.get("RASK_API_PREFIX")
    os.environ["RASK_API_PREFIX"] = config["RASK_API_PREFIX"]
    try:
        import notifications

        served = set(importlib.reload(notifications).app.openapi()["paths"])
    finally:
        if previous is None:
            os.environ.pop("RASK_API_PREFIX", None)
        else:
            os.environ["RASK_API_PREFIX"] = previous
        importlib.reload(importlib.import_module("notifications"))

    assert probed <= served, f"the kubelet probes {sorted(probed - served)}, which the service does not serve under {config['RASK_API_PREFIX']}"


def _notifications_cron_component(docs: list[dict]) -> dict:
    """The `bindings.cron` Component scoped to the notifications app-id."""
    for doc in docs:
        if doc.get("kind") == "Component" and (doc.get("spec") or {}).get("type") == "bindings.cron" and "notifications" in (doc.get("scopes") or []):
            return doc
    raise AssertionError("no bindings.cron Component is scoped to `notifications` — the /events reconciler would never tick")


def test_the_notifications_cron_binding_name_is_the_route_it_is_delivered_to() -> None:
    """The reconciler's Component name, its env, and the route the app serves are ONE string.

    Dapr delivers an input binding to `POST /<component name>` at the pod root. So a Component named
    one thing and a route mounted at another is a cron that fires into a 404 on every tick — with a
    healthy pod, a rendered Component, a running schedule, and nothing anywhere saying the deliveries
    are being dropped. The feed lane would simply never reconcile, which is indistinguishable from a
    feed that had nothing to reconcile.

    Guards all three corners at once, because any two of them can agree while the third drifts.

    The third corner runs in a SUBPROCESS, and that is load-bearing rather than tidy. The route path
    is bound at module import (`_binding = get_ingress_settings().binding_name`), so setting the env
    var in this process and calling `importlib.reload` proves nothing: reload re-executes the package
    `__init__` while `notifications.api.reconcile_cron` is already in `sys.modules`, so the pre-built
    router is re-included unchanged and the assertion silently degrades to "the chart default equals
    the code default". Worse, it then FAILS on a correct deployment the moment anyone edits
    `reconcileBindingName` — blaming the app for a chart change that a real pod, being a fresh
    process, honours. A subprocess IS that fresh process.
    """
    import os
    import subprocess
    import sys

    docs = _rendered_docs()
    component = _notifications_cron_component(docs)
    binding = component["metadata"]["name"]
    config = _fleet_config(docs)

    assert config.get("RASK_NOTIFICATIONS_BINDING_NAME") == binding, (
        f"the cron Component is named {binding!r} but the app is told {config.get('RASK_NOTIFICATIONS_BINDING_NAME')!r} — "
        "every tick would be delivered to a route the service does not serve"
    )

    probe = subprocess.run(
        [sys.executable, "-c", "import json,notifications; print(json.dumps(sorted(notifications.app.openapi()['paths'])))"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "RASK_NOTIFICATIONS_BINDING_NAME": binding, "RASK_API_PREFIX": config["RASK_API_PREFIX"]},
        cwd=REPO,
    )
    served = json.loads(probe.stdout)

    assert f"/{binding}" in served, f"the service serves no route at /{binding} — the cron Component ticks into a 404"


def test_the_notifications_reconciler_is_admitted_to_the_lineage_service_door() -> None:
    """The feed poll authenticates at lineage's SERVICE door, which is an explicit allowlist.

    `services.yaml` builds `LINEAGE_SERVICE_SUBJECTS` by scanning every service's own
    `env.RASK_LINEAGE_SERVICE_IDENTITY`, and the notifications service claims the same value. Omit the
    declaration and the reconciler 401s on every tick — a failure that reads as a credential problem
    while it is really the service never having been admitted at all.
    """
    docs = _rendered_docs()
    lineage = next(doc for doc in docs if doc.get("kind") == "Deployment" and (doc.get("metadata") or {}).get("name", "").endswith("-lineage"))
    env = {item["name"]: item.get("value") for item in lineage["spec"]["template"]["spec"]["containers"][0]["env"]}
    subjects = (env.get("LINEAGE_SERVICE_SUBJECTS") or "").split(",")

    container = _notifications_container(docs)
    claimed = {item["name"]: item.get("value") for item in container["env"]}.get("RASK_LINEAGE_SERVICE_IDENTITY")

    assert claimed, "the notifications service declares no RASK_LINEAGE_SERVICE_IDENTITY — its reconciler cannot reach the feed"
    assert claimed in subjects, f"lineage admits {subjects} but notifications claims {claimed!r} — every reconcile tick would 401"


def _lance_tracing_config(rendered: str) -> dict[str, Any] | None:
    """The one Dapr `Configuration` every sidecar references, or None if it did not render."""
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "Configuration" and doc.get("metadata", {}).get("name") == "lance-tracing":
            spec: dict[str, Any] = doc.get("spec") or {}
            return spec
    return None


def _retention_policy(spec: dict[str, Any]) -> dict[str, Any]:
    """The retention stanza, or an empty mapping. Narrowed rather than `or {}`-chained: the render is
    untyped YAML, so a `workflow` key holding something that is not a mapping must read as ABSENT here
    rather than raise an AttributeError that looks like a chart bug."""
    workflow = spec.get("workflow")
    if not isinstance(workflow, dict):
        return {}
    policy = workflow.get("stateRetentionPolicy")
    return policy if isinstance(policy, dict) else {}


def test_workflow_history_has_a_retention_policy() -> None:
    """Workflow history is append-only, and `stateRetentionPolicy` is the ONLY thing that collects it.

    Dapr's own CRD: "If not set, workflow instances will not be automatically purged." Nothing else in
    this estate purges them and nothing can — `DaprWorkflowClient` exposes `purge_workflow(id)` but no
    list-instances API, and `InMemoryRunStore` is documented as "deliberately NOT durable", so a sweep
    built on it would collect only what happened since the last pod start.

    Measured live 2026-08-10 before this landed: 1367 rows in `daprstate` for
    `dapr.internal.default.ingest.workflow`, `count(expiredate) = 0`, oldest row 2026-08-03 — every
    instance since the plane's first deploy, retained forever.
    """
    spec = _lance_tracing_config(_helm_template())

    assert spec is not None, "the Configuration every sidecar references did not render"
    policy = _retention_policy(spec)
    assert policy, "workflow history has no retention policy — it is retained forever"
    assert policy.get("completed"), "a COMPLETED run's history is never collected"
    assert policy.get("failed"), "a FAILED run's history is never collected"


def test_retention_does_not_disappear_when_telemetry_is_off() -> None:
    """THE REGRESSION THIS GUARDS, and it is the reason the Configuration is no longer otel-gated.

    A sidecar may reference exactly ONE `dapr.io/config`, so everything per-sidecar lives in one
    object — and that object used to render only when `lance.otelEnabled`. Retention is a DURABILITY
    concern, so hanging it there meant turning telemetry off silently returned the estate to unbounded
    workflow history. The gate now sits on the tracing stanza, which is the part that is about
    telemetry.
    """
    spec = _lance_tracing_config(_helm_template("observability.enabled=false"))

    assert spec is not None, "the Configuration vanished with telemetry — every sidecar's config reference now dangles"
    assert _retention_policy(spec), "retention was lost with telemetry"
    assert "tracing" not in spec, "tracing must still drop out when telemetry is off"


def test_every_sidecar_references_the_config_that_carries_retention() -> None:
    """A policy on an object nothing references governs nothing. Asserted with telemetry OFF, because
    that is the configuration in which the reference used to be omitted."""
    rendered = _helm_template("observability.enabled=false")

    assert 'dapr.io/config: "lance-tracing"' in rendered, "no sidecar references the retention config"
    assert _lance_tracing_config(rendered) is not None, "the referenced Configuration does not exist — a dangling reference"


def test_every_stream_has_its_retention_ASSERTED_not_merely_created() -> None:
    """§6 Q10 — retention is a fan-out correctness invariant, and creation alone cannot hold it.

    Both creation helpers in the stream Job are `if exists; then skip`, so a stream created ONCE with
    the wrong retention keeps it forever: the chart is right while the cluster is wrong, and nothing
    says so. That is not hypothetical — it was measured live on 2026-08-06, when INGEST was running
    `limits` while `queue.py` was built on `work`.

    What the wrong value costs is why this is a hard failure rather than a log line. `work` on a stream
    with several per-app durables means the FIRST ack deletes the message, so the apps steal deliveries
    from each other; `interest` empties the stream whenever no durable is attached, which is the exact
    state an ephemeral replay consumer depends on not happening. Neither surfaces as an error — both
    present as "some events did not arrive".

    Asserted on the rendered Job so a stream added without an assertion is caught HERE, not by a
    cascade that quietly under-runs.
    """
    job = _helm_template()
    marker = "assert_retention"

    assert marker in job, "the stream Job no longer asserts retention — a mis-created stream is undetectable again"
    # Every stream the Job creates must also be asserted. Parsed from the creation calls so a NEW
    # stream cannot be added with no assertion and still pass.
    created = {
        line.split()[1]
        for line in job.splitlines()
        if "_if_missing " in line and not line.strip().startswith("#") and len(line.split()) > 1 and line.split()[1].isupper()
    }
    asserted = {line.split()[1] for line in job.splitlines() if line.strip().startswith(f"{marker} ") and len(line.split()) > 1}
    asserted |= {"LINEAGE", "MEDALLION", "TRAINING", "DLQ", "CATALOG_CONTROL"} if "for stream in LINEAGE" in job else set()

    missing = created - asserted
    assert not missing, f"these streams are created but their retention is never asserted: {sorted(missing)}"


def test_inbound_retry_is_declared_on_the_COMPONENT_never_on_the_app() -> None:
    """§6 Q12(a) — the resiliency key that looks right and governs nothing.

    Dapr applies `targets.apps.<id>.retry` to SERVICE INVOCATION *out of* that app. Inbound pub/sub
    DELIVERY is governed only by `targets.components.<pubsub>.inbound.retry`. The two read almost
    identically in YAML, and putting a delivery policy under `apps` yields a CR that renders, validates
    and silently applies nothing to the thing it was written for — the cascade keeps whatever the
    component default is while the chart claims a policy.

    Asserted structurally on the rendered CR rather than by reading the file, so a future `apps` entry
    that grows an `inbound` block is caught here.
    """
    import yaml as _yaml

    rendered = _helm_template("dapr.resiliency.enabled=true")
    policies = [d for d in _yaml.safe_load_all(rendered) if d and d.get("kind") == "Resiliency"]

    assert policies, "no Resiliency CR rendered — this gate would pass vacuously"

    # COLLECTIVELY, not per-CR: the estate renders TWO — one for pub/sub (carrying `components`) and one
    # for service invocation (carrying `apps`). Asserting both properties of every CR fails the
    # invocation one for legitimately having no components, which is what the first version of this test
    # did.
    inbound_components: list[str] = []
    for cr in policies:
        targets = (cr.get("spec") or {}).get("targets") or {}
        for app_id, block in (targets.get("apps") or {}).items():
            assert "inbound" not in block, (
                f"targets.apps.{app_id} declares an `inbound` block. Inbound pub/sub delivery is governed "
                f"ONLY by targets.components.<pubsub>.inbound — this policy renders, validates, and applies "
                f"to nothing."
            )
        inbound_components += [name for name, block in (targets.get("components") or {}).items() if "inbound" in block]

    assert inbound_components, "no component anywhere declares an `inbound` policy — pub/sub delivery is ungoverned"
