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
    ("services/maintenance/src/maintenance/core/config.py", 'default="lineage.events.v1", alias="MAINTENANCE_LINEAGE_TOPIC"'),
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
