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

import functools
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.chart_render import ESO_ARGS


REPO = Path(__file__).resolve().parents[2]


CHART = REPO / "chart"


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
    # The identity values every render needs since auth defaults ON (2026-08-06), and the ESO APIs the
    # chart requires ([[XC-004]]). The chart refuses OIDC without a public issuer ON PURPOSE — that refusal
    # is what stops a forgotten values file installing an ungoverned estate. Supplying dev values HERE keeps
    # every other render test testing its own subject rather than re-testing the guard.
    argv += [*ESO_ARGS]
    argv += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    for value in set_values:
        argv += ["--set", value]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def test_no_SCOPED_storage_identity_is_published_with_the_ROOT_secret() -> None:
    """CONTRACT (security, standing rule): a scoped storage identity's secret is DERIVED, never the root.

    The estate mints scoped users and publishes each one's secret into OpenBao for ESO to sync. Four
    derived it from the root; `ray-compute` alone read
    `rayComputeSecretKey | default minio.secretKey` — and `rayComputeSecretKey` ships empty, so the
    published value WAS the RustFS root credential.

    MEASURED on the live estate 2026-09-11, by hash so nothing was disclosed: `ray-compute-secret-key`,
    `ray-compute-access-key` and `minio-secret-key` in `rask-infra-credentials` were byte-identical,
    all three 11 bytes. The chart CREATES `rask-ray-compute` with the derived secret
    (`minio-scoped-users.yaml`) and PUBLISHED the root one under its name, so the pair can never
    match: a consumer either signs as root — defeating the scoping entirely — or fails
    `SignatureDoesNotMatch`. The Ray head's own manifest asserts the opposite in a comment, which is
    how it survived.

    ASSERTED ON THE RENDER: the root is never in it ([[XC-004]]), so the dev OpenBao's seed writes each
    field from a file, and a scoped field must name the file `scoped_secret` derived for its identity,
    never the root's (`$G/minio-secret-key`). Generic over every scoped field the seed writes.
    """
    rendered = _helm_template("auth.bootstrapAdmin=user:gate-probe")

    fields = re.findall(r"([a-z-]*(?<!minio-)secret-key)=@\"([^\"]+)\"", rendered)
    assert fields, "the seed writes no scoped secret field, so this gate would pass vacuously"
    offenders = [f"{field}={source}" for field, source in fields if "/seed/scoped-" not in source]
    assert offenders == [], "a SCOPED identity is published with the ROOT storage secret:\n  " + "\n  ".join(offenders)


def test_every_first_party_deployment_is_hardened() -> None:
    """The docs claim "every Deployment has probes + preStop". The gateway had NEITHER (audit 2026-07-14).

    An "every" claim in prose is worth nothing; this loop is what makes it true. It renders the chart and
    checks each FIRST-PARTY Deployment (third-party subcharts — dapr/nats/openfga/dex — are not ours to
    template). preStop matters most on the gateway: it is the INGRESS, so without a drain delay a rolling
    update drops in-flight requests while kube-proxy is still routing to the terminating pod.

    IT NO LONGER NAMES ITS OWN SUBJECTS. This carried a hand-written tuple of ten name fragments —
    gateway, catalog, lineage, compaction, medallion-producer, the three stage runners, web, notifications —
    which omitted controlplane, compute, flows, ingest, maintenance, viewer, search and annotator. So a
    gate whose docstring argues that "an every claim in prose is worth nothing" made exactly that kind
    of claim with a literal list, and controlplane shipped with no preStop at all: a first-party
    Deployment serving `/api/projects` through the gateway, dropping in-flight project reads on every
    `helm upgrade` for as long as kube-proxy took to notice the endpoint removal.

    It now derives its subjects from the render (`_first_party_deployments`), so a NEW Deployment is
    checked by default rather than invisible until somebody remembers it. Per CONTAINER, too — the
    tuple version matched on the doc text, so a second container in a pod could satisfy the check for
    the first.
    """
    unhardened: list[str] = []
    for doc in _first_party_deployments(_rendered_docs("explorer.enabled=true")):
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            missing = [
                key
                for key, present in (
                    ("livenessProbe", "livenessProbe" in container),
                    ("readinessProbe", "readinessProbe" in container),
                    ("preStop", bool((container.get("lifecycle") or {}).get("preStop"))),
                )
                if not present
            ]
            if missing:
                unhardened.append(f"{name}/{container['name']} missing {missing}")
    assert not unhardened, f"first-party Deployments are not hardened: {unhardened}"


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


def _run_bucket_init(rendered: str, monkeypatch: pytest.MonkeyPatch) -> tuple[int, set[str]]:
    """Run the rendered bucket-init Job's own argv through `scripts/ensure_bucket.py`'s own `main` over an
    EMPTY moto store; return its exit code and the buckets the store then holds.

    BOTH HOPS IN ONE RUN: the render decides what the Job names and the script decides what happens to
    each name, so a check of either half alone passes while the other half drops a bucket.
    """
    from moto import mock_aws

    job = _job_by_component(rendered, "minio-mkbucket")
    assert job is not None, "the bucket-init Job does not render"
    command = yaml.safe_load(job)["spec"]["template"]["spec"]["containers"][0]["command"]
    assert command[:2] == ["python", "/srv/ensure_bucket.py"], f"the Job no longer runs the bucket script this test drives: {command[:2]}"

    spec = importlib.util.spec_from_file_location("ensure_bucket_under_test", REPO / "scripts" / "ensure_bucket.py")
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    # The Job's endpoint is the in-cluster Service, so the env chain must not point anywhere but moto.
    for name in ("RASK_S3_ENDPOINT_URL", "S3_ENDPOINT_URL", "HCP_ENDPOINT", "HCP_USERNAME", "HCP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    with mock_aws():
        import boto3

        exit_code = script.main(command[1:])
        present = {bucket["Name"] for bucket in boto3.client("s3", region_name="us-east-1").list_buckets()["Buckets"]}
    return exit_code, present


@pytest.mark.parametrize("observability", [False], ids=["observability-off"])
def test_the_bucket_init_creates_every_platform_bucket_and_every_zone(
    observability: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every bucket a value hands the bucket-init Job must exist once it has run on an EMPTY store.

    Each source is its own hop in the template, and a hop dropped is invisible while another value names
    the same bucket. So the root, the platform bucket and the zone here are each named by one value only.
    """
    rendered = _helm_template(
        "singleTenant.enabled=true",
        "explorer.enabled=true",
        f"observability.enabled={str(observability).lower()}",
        "minio.bucket=root-x",
        "minio.buckets={extra-platform}",
        "medallion.buckets.gold=acme-gold",
    )
    exit_code, present = _run_bucket_init(rendered, monkeypatch)

    assert exit_code == 0, f"the bucket-init Job fails on a store it provisioned itself (exit {exit_code}):\n{capsys.readouterr().err}"
    missing = {"root-x", "extra-platform", "acme-gold"} - present
    assert not missing, f"the Job left these buckets uncreated: {sorted(missing)}"


#: `observability.bucket` renamed with the two static values the render pairs it with: GreptimeDB's own
#: key and the storage registry's observability row. A `--set` list index replaces the whole list, so
#: both rows are restated.
_OBSERVABILITY_BUCKET_RENAMED = (
    "observability.bucket=obs-x",
    "greptimedb-standalone.objectStorage.s3.bucket=obs-x",
    "storage.stores[0].name=lance-catalog",
    "storage.stores[0].bucket=lance-catalog",
    "storage.stores[0].role=bronze",
    "storage.stores[1].name=obs-x",
    "storage.stores[1].bucket=obs-x",
    "storage.stores[1].role=observability",
)


@pytest.mark.parametrize("observability", [True, False], ids=["observability-on", "observability-off"])
def test_a_renamed_bucket_leaves_nothing_behind_under_its_old_name(
    observability: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rename the root and GreptimeDB's bucket: the Job makes each under its new name only, and GreptimeDB
    writes to the one it makes. A default spelled a second time in the values shows up here as a bucket
    the Job makes under the old name, which nothing uses.

    With observability off the platform set is empty, which is the render that hands the Job an empty
    name unless the template drops it.
    """
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    spellings = {"root-x", "obs-x", values["minio"]["bucket"], values["observability"]["bucket"]}
    wanted = {"root-x", "obs-x"} if observability else {"root-x"}
    rendered = _helm_template(
        "singleTenant.enabled=true",
        "explorer.enabled=true",
        f"observability.enabled={str(observability).lower()}",
        "minio.bucket=root-x",
        *_OBSERVABILITY_BUCKET_RENAMED,
    )
    exit_code, present = _run_bucket_init(rendered, monkeypatch)

    assert exit_code == 0, f"the bucket-init Job fails on a store it provisioned itself (exit {exit_code}):\n{capsys.readouterr().err}"
    assert present & spellings == wanted, (
        f"observability.enabled={observability}: the Job made {sorted(present & spellings)} of {sorted(spellings)}, wanted {sorted(wanted)}"
    )
    if observability:
        docs = [doc for doc in yaml.load_all(rendered, Loader=FAST_LOADER) if isinstance(doc, dict)]
        assert 'bucket = "obs-x"' in _greptimedb_config(docs), "GreptimeDB writes to a bucket the Job does not make"


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
    docs = [d for d in yaml.load_all(rendered, Loader=FAST_LOADER) if d]

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


#: A render whose images come from a registry off this node — which `prod-credentials.yaml` reads as a
#: real deployment, so it points at the platform's sealed store rather than the dev OpenBao. That keeps
#: a test about IMAGE PINNING testing its own subject.
_REAL_REGISTRY = (
    "image.localImages=false",
    "image.repository=reg.example",
    "openbao.devMode=false",
    "dex.enabled=false",
    "signing.provisioned=true",
    "nats.auth.provisioned=true",
)


def test_a_per_component_pin_beats_the_global_tag_and_a_digest_beats_the_pin() -> None:
    """#135 — the chart must be able to describe a fleet that is NOT one tag.

    One `image.tag` could not: a live estate ran a catalog tag across 11 services, a different tag on
    gateway/compute/controlplane, a third across the 7 zones, and an ingest DIGEST. Rendering all of
    that from one value is what made `helm upgrade` destructive — every image rewritten to a tag the
    node did not hold, whole fleet ImagePullBackOff, recovered by hand. Observed twice on 2026-08-06.

    Precedence is asserted end to end, because a pin that is merely ACCEPTED and then overridden is
    worse than no pin: it reads as safety while the deploy still rewrites the image.
    """
    pinned = _helm_template(*_REAL_REGISTRY, "image.tags.gateway=PINNED")
    assert 'image: "reg.example/gateway:PINNED"' in pinned, "a per-component tag did not reach the render"
    assert 'image: "reg.example/compute:dev"' in pinned, "the pin leaked onto a component that did not ask for it"

    # A digest is a CONTENT pin — a reconciler's, typically — so no tag may undo it.
    both = _helm_template(*_REAL_REGISTRY, "image.tags.gateway=IGNORED", "image.digests.gateway=sha256:abc")
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
    for doc in yaml.load_all(rendered, Loader=FAST_LOADER):
        if not doc or doc.get("kind") not in {"Deployment", "StatefulSet", "Job", "CronJob"}:
            continue
        spec = doc["spec"]["template"]["spec"] if doc["kind"] != "CronJob" else doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            names = [e["name"] for e in container.get("env", [])]
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                offenders.append(f"{doc['metadata']['name']}/{container['name']}: {sorted(dupes)}")
    assert offenders == [], f"duplicated env names make the release unupgradable: {offenders}"


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
    # THE RENDER HAS TO PRODUCE THE COMPONENTS THIS GATE CLAIMS TO CHECK. Stated as a property, the
    # rule is only as wide as the values it is rendered with: `maintenance.workTopic` and
    # `indexTopic` ship EMPTY, so the maintenance work-queue and index-build components did not
    # render at all and the gate passed over them vacuously — a property test blind to exactly the
    # lane whose units are the most expensive to lose. Set here rather than in values, because the
    # lane is a deployment choice and the gate must hold whether or not an estate makes it.
    rendered = _helm_template(
        "dapr.enabled=true",
        "dapr.resiliency.enabled=true",
        "maintenance.workTopic=maintenance.work.v1",
        "maintenance.indexTopic=maintenance.index.v1",
    )
    docs = [d for d in yaml.load_all(rendered, Loader=FAST_LOADER) if d]

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


def test_the_object_store_carries_NO_plaintext_credential() -> None:
    """The object store's OIDC client secret must be a `secretKeyRef`, never a `value:`.

    THE REGRESSION THIS GATES. `RUSTFS_IDENTITY_OPENID_CLIENT_SECRET` shipped as
    a plaintext `value:` — readable in `kubectl get statefulset -o yaml`, `kubectl describe` and
    `helm get manifest` — while every sibling credential in the estate was already behind a guard. It
    sat outside that guard because the object store has no daprd sidecar and so cannot read the Dapr
    secret store the fleet services use. That is the case the ESO-written `infra-credentials` exists
    for, and this asserts the store actually uses it.

    Latent-by-default is not a defence: `minio.oidc.enabled` is off in the shipped values, so this
    renders only on estates running STS credential vending — which is precisely where a leaked client
    secret is worth the most.
    """
    rendered = _helm_template("minio.enabled=true", "minio.oidc.enabled=true")
    docs = [d for d in yaml.load_all(rendered, Loader=FAST_LOADER) if d]

    stores = [d for d in docs if d.get("kind") == "StatefulSet" and any(c["name"] == "minio" for c in d["spec"]["template"]["spec"]["containers"])]
    assert stores, "the object store did not render — this gate would pass vacuously"

    for store in stores:
        for container in store["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env") or []:
                if not str(env.get("name", "")).endswith("_CLIENT_SECRET"):
                    continue
                assert "value" not in env, f"{env['name']} renders a PLAINTEXT value on the store: {env!r}"
                ref = ((env.get("valueFrom") or {}).get("secretKeyRef")) or {}
                assert ref.get("name") and ref.get("key"), f"{env['name']} has neither a value nor a usable secretKeyRef: {env!r}"

                # The reference must RESOLVE — a secretKeyRef at an absent key is a pod that never
                # starts, and it would only surface on the enabled path, which is the narrowest
                # possible place to discover it.
                written = _written_secrets(docs)
                keys = written.get(ref["name"])
                assert keys is not None, f"{env['name']} references Secret {ref['name']!r}, which nothing the chart renders writes"
                assert ref["key"] in keys, f"{env['name']} references key {ref['key']!r}, absent from Secret {ref['name']!r} (has {sorted(keys)})"


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
            for doc in yaml.load_all(rendered, Loader=FAST_LOADER)
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


#: The parsed chart, memoised on the render's arguments and parsed by libyaml.
#:
#: BOTH HALVES ARE PURE COST REMOVAL, measured 2026-09-09 against the real chart. The render is a pure
#: function of its `--set` arguments and the chart does not change inside a session, yet a full
#: `tests/unit` run made >=278 `helm template` invocations for only ~146 distinct argument sets — over
#: half of them byte-identical recomputations, ~123s of subprocess thrown away. And helm was never the
#: larger half: one render is 0.45s while `yaml.safe_load_all` over its 2.3 MB / 293-document output is
#: 1.50s, against 0.15s for the `CSafeLoader` that was already installed in this venv and referenced
#: nowhere in the repo — a 10x penalty, paid on every parse, ~400s of the directory's ~730s.
#:
#: CACHED HERE AND NOT ON `_helm_template`, deliberately: three of that function's callers need a real
#: subprocess each time — one renders a DIFFERENT chart path out of a tmpdir, one passes `check=False`
#: and asserts on the returncode, one expects `CalledProcessError`. Every caller of THIS function takes
#: the default chart and the successful path, which is what makes memoising it safe.
#:
#: The list is returned by reference, so a caller that MUTATES the documents would corrupt every later
#: caller. None does — these gates read the render and assert on it.
@functools.cache
def _rendered_docs(*set_values: str) -> list[dict]:
    return [doc for doc in yaml.load_all(_helm_template(*set_values), Loader=FAST_LOADER) if isinstance(doc, dict)]


def _greptimedb_config(docs: list[dict]) -> str:
    """The telemetry store's rendered `config.toml`, as text.

    Located by CONTENT, not by name: the subchart names its ConfigMap
    `<release>-greptimedb-standalone-config`, and a release-name change would silently return {} from a
    name match, turning every gate below green against a config it could no longer find.
    """
    for doc in docs:
        data = doc.get("data") or {}
        toml = data.get("config.toml", "")
        if doc.get("kind") == "ConfigMap" and "[storage]" in toml and "greptimedb" in toml:
            return toml
    return ""


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


def test_the_ray_address_names_a_service_the_chart_actually_creates() -> None:
    """`ray-lance-head` was the hardcoded default and does not exist in a KubeRay deployment.

    Measured 2026-08-15 from inside a pod: `ray-lance-head` fails DNS, `rask-ray-head-svc` answers
    `/api/version` with ray 2.56.1. The old value was the on-kind demo's raw head, and every stage runner
    would have submitted into a hostname that does not resolve — a failure that surfaces only when a
    trigger arrives.

    Derived from the release name rather than pinned, and pointing at the STABLE head service: the
    RayCluster KubeRay owns carries a generated suffix (`rask-ray-22nls`) that no chart can name and
    that changes on re-provision.

    RENDERED WITH THE LANE FORCED ON, because the default is now off (no Lance-capable cluster exists
    — see `test_the_ray_lane_is_OFF_until_a_LANCE_CAPABLE_cluster_exists`) and the address is only
    emitted when it is on. The property under test is what the value SAYS when it is present, so the
    fixture has to produce one; asserting against the default would silently test nothing.
    """
    docs = _rendered_docs("medallion.ray=true")
    services = {(doc.get("metadata") or {}).get("name") for doc in docs if doc.get("kind") == "Service"}
    addresses = {
        e.get("value")
        for doc in docs
        if doc.get("kind") == "Deployment"
        for c in doc["spec"]["template"]["spec"]["containers"]
        for e in (c.get("env") or [])
        if e.get("name") == "MEDALLION_RAY_ADDRESS"
    }
    assert addresses, "no stage runner declares MEDALLION_RAY_ADDRESS"
    for address in addresses:
        host = str(address).removeprefix("http://").split(":")[0]
        assert host.endswith("-ray-head-svc"), f"{address} does not name KubeRay's stable head service"
        assert "{{" not in str(address), "values.yaml is not templated — a {{ }} default ships literal braces"
        # The RayService creates it, so it is absent from the rendered docs when ray.enabled is off —
        # assert the SHAPE unconditionally and the existence only when the chart renders Ray at all.
        if any(str(s).endswith("-ray-head-svc") for s in services):
            assert host in services, f"{host} is not a Service this chart creates"


# `test_the_kubelet_probes_the_inbox_on_a_path_the_service_actually_serves` lived here and is GONE,
# subsumed rather than deleted for tidiness: `tests/unit/test_probe_paths_are_served.py` asks the same
# question — is every path the chart probes one the app actually mounts — of every first-party app in
# the render, under that container's own chart env, instead of notifications alone. Keeping both would
# leave two mechanisms for one claim, and the weaker one was the reason the audit filed
# "the probe-path-is-actually-served gate covers exactly one of the fifteen apps".


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


def _lance_tracing_config(rendered: str) -> dict[str, Any] | None:
    """The one Dapr `Configuration` every sidecar references, or None if it did not render."""
    for doc in yaml.load_all(rendered, Loader=FAST_LOADER):
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


def test_the_sidecars_non_telemetry_config_survives_telemetry_being_off() -> None:
    """THE REGRESSION THIS GUARDS, and it is the reason the Configuration is no longer otel-gated.

    A sidecar may reference exactly ONE `dapr.io/config`, so everything per-sidecar lives in one
    object — and that object used to render only when `lance.otelEnabled`. Retention is a DURABILITY
    concern, so hanging it there meant turning telemetry off silently returned the estate to unbounded
    workflow history. The gate now sits on the tracing stanza, which is the part that is about
    telemetry.

    RENAMED AND WIDENED rather than joined by a sibling, because retention was only the FIRST instance
    of the rule. Metric cardinality and api-logging are instances two and three: the sidecar's `:9090`
    exposition and its log stream both exist whether or not this estate ships a collector, so a bound
    that vanishes with telemetry is not a bound. A second test asserting the same rule from the same
    render would be a divergent answer to a settled question.

    ASSERTING THE LITERAL KEY NAMES IS THE WHOLE POINT, and it is the only gate there is. Helm does not
    request strict field validation, so `increasedCardinallity`, `recordErrorCode` or `obfuscateUrls`
    would render, apply CLEANLY, and be silently pruned by the API server — no error, no warning, no
    chart-visible signal. The symptom is "the setting had no effect", which is the same shape as the
    NullExporter defect this file already guards. Measured against the live k3s CRD: a strict-validating
    client rejects those three, and `--validate=ignore` (what Helm gets) stores the object with every
    typo dropped.

    `metric` (singular) is a real CRD key with a byte-identical schema and no stated precedence; daprd
    merges plural over singular field-by-field, so writing both would make that merge the only tiebreak.
    Plural only, asserted.
    """
    spec = _lance_tracing_config(_helm_template("observability.enabled=false"))

    assert spec is not None, "the Configuration vanished with telemetry — every sidecar's config reference now dangles"
    assert _retention_policy(spec), "retention was lost with telemetry"
    assert "tracing" not in spec, "tracing must still drop out when telemetry is off"

    metrics = spec.get("metrics")
    assert isinstance(metrics, dict), (
        f"no spec.metrics stanza with telemetry off (keys: {sorted(spec)}) — the sidecar still scrapes at :9090, so its cardinality bound must not be otel-gated"
    )
    assert "metric" not in spec, (
        "the legacy singular `metric:` key is also set — daprd merges the two field-by-field, so the manifest no longer says what is in force"
    )
    assert metrics.get("enabled") is True, "spec.metrics.enabled is REQUIRED by the CRD — without it the API server refuses the object outright"
    assert metrics.get("recordErrorCodes") is True, (
        "recordErrorCodes must sit under `metrics:`, NOT under `metrics.http:` — under http it is an unknown field and is pruned in silence"
    )

    http = metrics.get("http")
    assert isinstance(http, dict), "spec.metrics.http is missing"
    assert http.get("increasedCardinality") is False, (
        "increasedCardinality is not pinned false. At the upstream default the sidecar stamps the raw remainder of a "
        "service-invocation URL onto the `path` label — measured live: "
        'dapr_http_server_request_count{app_id="gateway",path="/v1.0/invoke/compute/method/api/ray/jobs"} — which is one '
        "series per table, namespace and project id, forever."
    )

    patterns = http.get("pathMatching")
    assert isinstance(patterns, list), "pathMatching must be a YAML sequence of strings; a map is refused by the API server"
    assert patterns, "pathMatching is empty, so every path collapses to the empty label"
    for pattern in patterns:
        assert not pattern.startswith("/v1.0/invoke/"), (
            f"{pattern!r} is a service-invocation pattern. daprd auto-registers an invoke twin for every OTHER entry here "
            "and registers the lot on a real http.ServeMux, which PANICS at startup on conflicting patterns. One "
            "Configuration serves every app-id, so that is the whole fleet crash-looping — triggered by a later one-line "
            "edit adding `/` or a bare `/{x...}` to this same list."
        )

    api_logging = (spec.get("logging") or {}).get("apiLogging")
    assert isinstance(api_logging, dict), "spec.logging.apiLogging is missing"
    assert api_logging.get("obfuscateURLs") is True, (
        "obfuscateURLs must be true and is INSEPARABLE from apiLogging.enabled: with it false daprd logs the raw "
        "`method + URL.Path`, and the actor URL carries base64url(<oidc sub>), which `decode_subject` reverses exactly. "
        "Enabling api logging without it would CREATE a subject exposure."
    )
    assert api_logging.get("omitHealthChecks") is True, "the kubelet polls /v1.0/healthz on every sidecar; without this the stream is mostly probe noise"


def test_what_the_producer_PUBLISHES_is_what_a_stage_runner_ACCEPTS() -> None:
    """A lane whose two halves disagree fails with a 200 OK and no log line anywhere.

    The producer stamps `bronzeDataset` / `bronzeNamespace` on the trigger it publishes to
    `bronzeTopic`. The stage runner subscribed to that topic compares the claim against its own
    `fromDataset` / `fromNamespace` and, on a mismatch, returns DROP — which is a SUCCESS ack. The
    wire looks healthy end to end:

        stage runner      POST /medallion-event  200 OK      <- the app accepted the delivery
        stage runner      POST /dlq-event        200 OK      <- and immediately dead-lettered it
        daprd      "DROP status returned from app while processing pub/sub event ..."

    and the stage runner's own log says NOTHING. Measured 2026-08-25, after the tiers were nested: the
    producer still published `bronze$events` while every stage runner had moved to `lakehouse$bronze$events`,
    so the cascade died at the first hop and the only evidence was one warning in a SIDECAR log.

    Pairing them here because they are rendered from different values by different templates and
    nothing else compares them: they are one contract written in two places.
    """
    import yaml as _yaml

    values = _yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    medallion = values.get("medallion") or {}
    producer = medallion.get("producer") or {}
    stage_runners = medallion.get("stageRunners") or []
    assert stage_runners, "no stage_runners declared — this gate is now blind"

    topic = producer.get("bronzeTopic")
    assert topic, "the producer declares no bronzeTopic, so nothing can consume its writes"

    consumers = [m for m in stage_runners if m.get("subTopic") == topic]
    assert consumers, (
        f"the producer publishes to {topic!r} and no stage runner subscribes to it — the head writes bronze "
        f"and the cascade never starts, with every hop reporting success."
    )

    mismatched = []
    for stage_runner in consumers:
        for producer_key, stage_runner_key in (("bronzeDataset", "fromDataset"), ("bronzeNamespace", "fromNamespace")):
            want, got = producer.get(producer_key), stage_runner.get(stage_runner_key)
            if want != got:
                mismatched.append(f"  {stage_runner.get('name')}: producer.{producer_key}={want!r} but stage_runner.{stage_runner_key}={got!r}")

    assert not mismatched, (
        f"the producer publishes a lane no stage_runner on {topic!r} accepts:\n" + "\n".join(mismatched) + "\n\n"
        "The stage runner DROPs a trigger whose lane claim does not match its own, and DROP acks as success — "
        "so this fails with 200 OK on every hop and no error in the stage runner's log. Rename BOTH halves or "
        "neither."
    )

    # The media chain is the same contract through a different pair of values, and it drifted the same
    # way for the same reason: the URI was a literal in the template while the stage runner's had moved.
    media_ns = medallion.get("mediaBronzeNamespace")
    media_consumers = [m for m in stage_runners if m.get("operation") == "derive_media"]
    for stage_runner in media_consumers:
        assert media_ns == stage_runner.get("fromNamespace"), (
            f"medallion.mediaBronzeNamespace={media_ns!r} but the media stage_runner reads "
            f"{stage_runner.get('fromNamespace')!r} — the head lands blobs where nothing is listening, and the "
            f"trigger is DROPped with a 200 OK."
        )
        # The DATASET is the half that actually gets compared, and it is the half the chart forgot:
        # MEDALLION_MEDIA_BRONZE_DATASET was rendered nowhere, so the head fell back to the flat code
        # default `bronze-media$objects` while the stage runner had moved. The template derives it as
        # `<namespace>$objects`; assert the same derivation rather than trusting it.
        assert f"{media_ns}$objects" == stage_runner.get("fromDataset"), (
            f"the media head stamps dataset {media_ns}$objects but the stage runner accepts "
            f"{stage_runner.get('fromDataset')!r} — a lane mismatch DROPs with a 200 OK and logs nothing."
        )


def test_a_medallion_NAMESPACE_can_actually_belong_to_a_warehouse() -> None:
    """With warehouses on, a flat tier belongs to nothing — and a PRE-qualified one gets qualified twice.

    `require_warehouse_scoped` refuses a top-level namespace that belongs to no warehouse, and every
    bucket a medallion tier could resolve to is reserved platform storage no warehouse may back (the
    catalog root in-app; anything in `medallion.buckets` by the chart, which appends that map into
    `LANCE_RESERVED_BUCKETS`). So a bare `bronze` is unownable. Two shapes escape that, and an estate
    must be in one of them:

    * **Project-qualified** (`medallion.projectsEnabled`) — `project_namespace` prefixes `<project>-`
      at RUNTIME (`transform.resolve_stage_identity`), so `bronze` becomes `acme-bronze`: still
      top-level, but owned by that project's warehouse. This is the shape `seed_estate.py` has always built.
    * **Nested** (`<parent>$bronze`) — the guard returns early for `len(segments) > 1`, so a child
      inherits its parent's warehouse and only the parent is bound.

    THE TRAP IS DOING BOTH. `project_namespace` prefixes every declared name unconditionally, so a
    nested one is qualified too. Measured live 2026-08-25 on an estate whose project was `lakehouse`
    and whose tiers had been nested under a parent also called `lakehouse`:

        POST /v1/table/lakehouse-lakehouse$gold$catalog/create -> 403

    Every silver→gold hop failed on a table id that can never exist, and the stage runner reported only
    `medallion_stage_failed`. So with projects ON the declaration must stay UNQUALIFIED and unnested —
    the runtime owns the qualification, and pre-empting it doubles it.
    """
    import yaml as _yaml

    values = _yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    medallion = values.get("medallion") or {}
    warehouses_on = bool(((values.get("catalog") or {}).get("warehouses") or {}).get("enabled"))
    projects_on = bool(medallion.get("projectsEnabled"))
    delimiter = (values.get("catalog") or {}).get("delimiter") or "$"

    # The namespaces are the seeder's own derivation, so the gate and the seed cannot read the chart two
    # ways. It indexes `stageRunners` strictly and reads `mediaStageRunners[]` too: a renamed key fails here.
    import sys

    spec = importlib.util.spec_from_file_location("seed_medallion_namespaces_under_test", REPO / "scripts" / "seed_medallion_namespaces.py")
    assert spec is not None and spec.loader is not None
    seeder = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = seeder
    spec.loader.exec_module(seeder)
    declared: list[str] = seeder.declared_namespaces([CHART / "values.yaml"])
    assert declared, "no medallion namespaces declared — this gate is now blind"

    if projects_on:
        # The runtime qualifies. A declaration that is already nested (or already prefixed) is what
        # produces the doubled id above, so it is refused regardless of the warehouse setting.
        doubled = [ns for ns in declared if delimiter in ns]
        assert not doubled, (
            "medallion.projectsEnabled is true, so `project_namespace` prefixes `<project>-` at runtime — "
            "and these namespaces are already nested, so they get qualified ANYWAY:\n  " + "\n  ".join(doubled) + "\n\n"
            "The result is `<project>-<parent>$<tier>`, a table id nothing can create — every hop 403s and "
            "the stage runner logs only `medallion_stage_failed`. Declare the bare tier name and let the runtime "
            "qualify it."
        )
        return

    if not warehouses_on:
        return  # single-bucket: the shared root is the correct destination

    flat = [ns for ns in declared if delimiter not in ns]
    assert not flat, (
        "catalog.warehouses.enabled is true and medallion.projectsEnabled is false, so nothing qualifies "
        "these names and a top-level namespace must belong to a warehouse — but every bucket they could "
        "resolve to is reserved platform storage no warehouse may back:\n  " + "\n  ".join(flat) + "\n\n"
        "Turn on medallion.projectsEnabled (the runtime then owns `<project>-<tier>`, owned by that "
        f"project's warehouse), nest them under one bound parent ('<parent>{delimiter}<tier>'), or run "
        "single-bucket with catalog.warehouses.enabled=false."
    )


def test_ray_serve_is_actually_IMPORTABLE_from_the_root_lock() -> None:
    """Declaring `ray[serve]` is not the same as being able to import it, and the gap is upstream.

    The sibling gate below checks the DECLARATION. That would not have caught this, because the
    declaration was already correct: `ray[data,default,serve]` resolved 20 packages and the image
    still died at

        ModuleNotFoundError: No module named 'jinja2'.
        You can run `pip install "ray[serve]"` to install all Ray Serve dependencies.

    an error that names the extra it is already installing. Ray 2.58.0 declares jinja2 in NO extra
    (verified against ray-2.58.0.dist-info/METADATA: 69 Requires-Dist lines carry `extra == "serve"`,
    none of them jinja2) while `ray/serve/_private/haproxy.py:19` does `from jinja2 import Environment`
    at module load — so `import ray.serve` fails on a correctly-declared install.

    This asserts the thing that actually matters: the ROOT LOCK, which is what
    `.docker/ray-cluster.dockerfile` syncs, can import the module the KubeRay operator's dashboard
    query depends on. An upstream extra that silently loses a dependency is caught here rather than by
    a stalled cluster upgrade nobody is watching.
    """
    import importlib

    module = importlib.import_module("ray.serve")
    assert module is not None

    # The dashboard's Serve endpoint builds this model; importing the package alone does not prove the
    # schema path is intact, and it is the schema path the operator's GetServeDetails exercises.
    schema = importlib.import_module("ray.serve.schema")
    assert hasattr(schema, "ServeInstanceDetails"), "ray.serve.schema is missing ServeInstanceDetails — the operator's GetServeDetails would 500"


#: Every deployed app and the env var its own config reads for the docs opt-in. The names differ
#: because the services predate any shared setting; what must NOT differ is that each one is set.
_DOCS_ENV_BY_WORKLOAD: dict[str, str] = {
    "catalog": "LANCE_REST_DOCS",
    "lineage": "LINEAGE_DOCS",
    "medallion-producer": "MEDALLION_DOCS",
    "maintenance": "MAINTENANCE_DOCS",
    "viewer": "MEDIA_DOCS",
    "search": "MEDIA_DOCS",
    "annotator": "MEDIA_DOCS",
}


def _env_of(container: dict) -> dict[str, str]:
    return {e["name"]: str(e.get("value", "")) for e in (container.get("env") or []) if "name" in e}


def _app_containers(docs: list[dict]) -> dict[str, dict]:
    """Each workload's app container, keyed by the workload name it renders under."""
    found: dict[str, dict] = {}
    for doc in docs:
        if doc.get("kind") not in {"Deployment", "StatefulSet"}:
            continue
        name = doc["metadata"]["name"]
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            env = _env_of(container)
            for workload, var in _DOCS_ENV_BY_WORKLOAD.items():
                if name.endswith(workload) and var in env:
                    found[workload] = container
    return found


def test_the_chart_TURNS_DOCS_OFF_by_default_and_ON_when_asked() -> None:
    """Interactive docs must be a deployment decision, and the deployment must actually make it.

    open_fastapi-audit — "/docs and /openapi.json are on in production for every served app".

    The code defaults are closed now, which is the load-bearing half. This is the other half, and it
    is the one the finding is really about: four services ALREADY carried a `docs_enabled` flag and
    every one of them shipped docs anyway, because no deployment path ever set it —
    `grep -rn DOCS chart/ .docker/ scripts/` matched nothing but an unrelated `_DOCS = _ROOT / "docs"`.
    A flag no manifest sets is not a control, it is a comment.

    So the gate asserts the env var is RENDERED, in both positions. Asserting only the "off" case
    would pass just as well against a chart that never mentions docs at all — which is exactly the
    state this finding describes.
    """
    # `explorer.enabled` is false by default (a fresh cluster must come up with no node
    # preparation), so viewer/search/annotator render only when asked for. Enable it here or the
    # gate silently covers four of the seven apps.
    _ON = "explorer.enabled=true"
    off = _app_containers(_rendered_docs(_ON))
    missing = sorted(set(_DOCS_ENV_BY_WORKLOAD) - set(off))
    assert not missing, (
        f"these workloads render no docs env var at all: {missing} — a service whose docs flag no manifest sets is one whose default nobody chose"
    )
    for workload, container in off.items():
        var = _DOCS_ENV_BY_WORKLOAD[workload]
        assert _env_of(container)[var].lower() in {"false", "0"}, f"{workload} renders {var}={_env_of(container)[var]} by default — docs are opt-in"

    on = _app_containers(_rendered_docs(_ON, "docs.enabled=true"))
    for workload in _DOCS_ENV_BY_WORKLOAD:
        var = _DOCS_ENV_BY_WORKLOAD[workload]
        value = _env_of(on[workload])[var]
        assert value.lower() in {"true", "1"}, f"docs.enabled=true left {workload} at {var}={value} — a knob that cannot open is a deletion"


#: Deployments this gate does NOT cover, enumerated from an actual render rather than guessed.
#: Matched as substrings of the rendered name, like the sibling gates in this file.
#:
#: Two different reasons, kept apart on purpose:
#:
#:  * SUBCHARTS — dapr, nats, openfga, dex, cloudnative-pg, openbao, rustfs, kueue, kuberay,
#:    greptimedb, perses, vmalert, alertmanager. Not ours to template, so not ours to harden.
#:  * INFRA PODS WE DO TEMPLATE — the OTel collector. Its securityContext is gated behind
#:    `security.infraContexts.enabled`, which values.yaml defaults OFF and stages explicitly:
#:    "both default OFF (behavior-identical); flip after the §7a live checks". That is a known,
#:    sequenced decision with its own live-check step, not an oversight, so this gate must not
#:    silently pre-empt it. It is listed HERE, visibly, rather than being missing from a
#:    hand-written first-party tuple where nobody could tell the difference.
_UNCOVERED_DEPLOYMENTS = (
    "dapr", "nats", "openfga", "dex", "cloudnative-pg", "openbao", "minio", "kueue",
    "kuberay", "greptimedb", "perses", "vmalert", "alertmanager",
    "otel-collector",
)  # fmt: skip


def _first_party_deployments(docs: list[dict]) -> list[dict]:
    """Every rendered Deployment that is OURS, derived rather than listed.

    The gate this replaces carried a hand-written tuple of ten names, and the cost of that is not
    hypothetical: `compute`, `controlplane`, `flows` and `ingest` were simply absent from it, so the
    hardening gate skipped four first-party pods silently — a new Deployment was covered only if
    somebody remembered to add it. Deriving the set means the default is COVERED and an exemption has
    to be argued for by name.
    """
    return [doc for doc in docs if doc.get("kind") == "Deployment" and not any(skip in doc["metadata"]["name"] for skip in _UNCOVERED_DEPLOYMENTS)]


def test_a_read_only_rootfs_is_SURVIVABLE_on_every_first_party_pod() -> None:
    """Hardening that breaks the pod is not hardening, it is a rollback waiting to happen.

    `readOnlyRootFilesystem: true` is the chart default, and these images write to /tmp for OTel and
    pyarrow exactly as the lance ones do. The scratch pair exists for this (`lance.tmpMount` +
    `lance.tmpVolume`, bounded by `security.tmpSizeLimit`); a pod that gets the securityContext
    without the volume crash-loops on its first spill instead of on the next deploy.
    """
    naked: list[str] = []
    for doc in _first_party_deployments(_rendered_docs()):
        spec = doc["spec"]["template"]["spec"]
        volumes = {vol["name"] for vol in (spec.get("volumes") or [])}
        for container in spec.get("containers") or []:
            if not (container.get("securityContext") or {}).get("readOnlyRootFilesystem"):
                continue
            mounts = {m["mountPath"] for m in (container.get("volumeMounts") or [])}
            if "/tmp" not in mounts or "tmp" not in volumes:
                naked.append(f"{doc['metadata']['name']}/{container['name']}")

    assert not naked, f"read-only rootfs with no writable /tmp: {sorted(naked)}"


def test_the_media_head_can_register_the_bronze_it_lands() -> None:
    """`/ingest-media` must be able to govern its own tier on every shipped configuration.

    The catalog ADDRESS is the first half and the test above pins it — `/ingest-media` is served by the
    same producer pod as `/produce`, so it inherits that fix. The second half is unique to this lane and
    invisible from the template: `register_table` addresses only paths INSIDE the root the catalog is
    connected to, so `MEDALLION_MEDIA_BRONZE_URI` must resolve under `MEDALLION_CATALOG_ROOT` or the
    media head fails closed on every call. The two are rendered from DIFFERENT expressions —
    `lance.stageBucket` honours the `medallion.buckets` zoning map for the media namespace, while the
    catalog root is `minio.bucket` flat — so zoning `bronze-media` into its own bucket would make the
    ingest door 503 with nothing in the chart looking wrong. `relative_location` is the exact seam the
    producer uses, so this asserts registerability rather than a string resemblance.
    """
    import yaml

    from medallion.services.catalog_register import RegisterError, relative_location

    for review in ("false", "true"):
        rendered = _helm_template(f"medallion.qualityReview={review}")
        producer = next(
            (
                doc
                for doc in yaml.load_all(rendered, Loader=FAST_LOADER)
                if doc and doc.get("kind") == "Deployment" and "medallion-producer" in doc["metadata"]["name"]
            ),
            None,
        )
        assert producer is not None, f"the medallion producer did not render at qualityReview={review}"
        env = {item["name"]: item.get("value", "") for item in producer["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        missing = {"MEDALLION_MEDIA_BRONZE_URI", "MEDALLION_CATALOG_ROOT", "MEDALLION_CATALOG_URL"} - set(env)
        assert not missing, f"at medallion.qualityReview={review} the media head cannot register what it lands: {sorted(missing)} not rendered"
        try:
            location = relative_location(env["MEDALLION_MEDIA_BRONZE_URI"], env["MEDALLION_CATALOG_ROOT"])
        except RegisterError as exc:  # noqa: PERF203 — one render per flag state, not a hot loop
            pytest.fail(
                f"at medallion.qualityReview={review} the shipped chart lands media bronze somewhere the catalog cannot name, "
                f"so POST /ingest-media fails closed on every call: {exc}"
            )
        assert location, "the media bronze URI resolved to the catalog root itself — a tier must have its own location"


def _env_by_component(docs: list[dict], component: str) -> dict[str, str]:
    """The merged container env of the Deployment carrying ``app.kubernetes.io/component: <component>``.

    Selected by LABEL rather than by name because `_helm_template` renders without a release name, so a
    name-prefix match would encode helm's `release-name` placeholder.
    """
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        labels = ((doc["spec"]["template"].get("metadata") or {}).get("labels")) or {}
        if labels.get("app.kubernetes.io/component") != component:
            continue
        env: dict[str, str] = {}
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            env |= _env_of(container)
        return env
    return {}


def test_no_workload_references_a_secret_the_render_does_not_create() -> None:
    """A `secretKeyRef` to a Secret nothing creates is a pod that never starts, and a GREEN render.

    THE FAILURE THIS PINS. `dapr.sidecars=false` is a documented, supported toggle, and
    `services.yaml`'s own fail message tells an operator to pair it with `catalog.controlEmit=false`.
    That pair rendered cleanly — and left THIRTEEN Deployments (all seven zones, maintenance, the
    producer, three stage runners and lineage) carrying a reference to `-dapr-app-token`, which is gated on
    `dapr.sidecars` and therefore absent. Each fails with CreateContainerConfigError; nothing in the
    render says why.

    The cause was one Secret doing double duty under two different gates: the Dapr app token
    (`dapr.sidecars`) and `LINEAGE_SERVICE_TOKEN`, the service credential for reading lineage
    (`auth.enabled`).

    Checked across the TOGGLE COMBINATIONS, not just the default render, because the default is the one
    configuration this class cannot appear in — every consumer and its Secret are on together there.
    """
    combinations = [
        ("default", []),
        ("sidecars off", ["dapr.sidecars=false", "catalog.controlEmit=false"]),
        ("auth off", ["auth.enabled=false"]),
        ("both off", ["dapr.sidecars=false", "catalog.controlEmit=false", "auth.enabled=false"]),
    ]
    problems: list[str] = []
    for label, extra in combinations:
        docs = _rendered_docs(*extra)
        secrets = set(_written_secrets(docs))
        # SUBCHART NAMING is a separate concern and not this gate's. A subchart names its workloads
        # `{{ .Release.Name }}-x` while this chart's own Secrets use `lance.fullname`, so the two agree
        # only when the release is named `rask` — which it always is here, and which `_helm_template`
        # does not pin. Filtered rather than asserted, so this gate reports the class it was built for
        # instead of a rendering artefact; the naming mismatch is recorded in the backlog.
        secrets |= {name.replace("release-name-", "rask-", 1) for name in secrets}
        for doc in docs:
            for name in _secret_refs(doc):
                # A Dapr Component's secretKeyRef names a key in the SECRET STORE (OpenBao), not a
                # Kubernetes Secret — a different namespace of names entirely.
                if doc.get("kind") == "Component" or name in secrets:
                    continue
                problems.append(f"[{label}] {doc.get('kind')}/{doc['metadata']['name']} -> missing Secret {name!r}")
    assert not problems, "workloads reference Secrets the render never creates:\n  " + "\n  ".join(sorted(set(problems)))


def _written_secrets(docs: list[dict]) -> dict[str, set[str]]:
    """Each Secret the render creates, with its keys: a chart-rendered Secret, or the target an
    ExternalSecret writes ([[XC-004]]: every credential reaches a pod without a sidecar that way)."""
    written: dict[str, set[str]] = {}
    for d in docs:
        if d.get("kind") == "Secret":
            written[d["metadata"]["name"]] = set(d.get("stringData") or {}) | set(d.get("data") or {})
        elif d.get("kind") == "ExternalSecret":
            target = d["spec"].get("target") or {}
            keys = set((target.get("template") or {}).get("data") or {}) or {e["secretKey"] for e in d["spec"].get("data") or []}
            written[target.get("name") or d["metadata"]["name"]] = keys
    return written


def _secret_refs(doc: object) -> list[str]:
    """Every `secretKeyRef` name anywhere in a rendered document."""
    found: list[str] = []
    if isinstance(doc, dict):
        ref = doc.get("secretKeyRef")
        if isinstance(ref, dict) and ref.get("name"):
            found.append(str(ref["name"]))
        for value in doc.values():
            found.extend(_secret_refs(value))
    elif isinstance(doc, list):
        for value in doc:
            found.extend(_secret_refs(value))
    return found


def test_the_catalog_STAGES_its_write_announcement_into_the_prefix_the_relay_DRAINS() -> None:
    """The catalog's lineage outbox must be rendered, and must name the prefix the lineage relay reads.

    `LANCE_LINEAGE_OUTBOX_URI` exists in `catalog/core/config.py` and is threaded all the way to
    `outbox.publish_lineage_with_outbox` in `lineage_emit.py` — and rendered NOWHERE, so on every shipped
    chart the catalog's emit degraded to the pre-#4 plain publish. This is the same failure class as the
    `MEDALLION_LINEAGE_OUTBOX_URI` dead-env above, mirrored: there the chart set what no code read, here
    the code reads what no chart sets. Only a RENDER can tell either of them apart from a working feature.

    What it costs on the catalog specifically: the emit is inline-awaited and best-effort AFTER the Lance
    write commits, and medallion's `/bronze-arrival` subscription reacts to that announcement — so a lost
    publish does not merely under-report provenance, the whole bronze->silver->gold run silently never
    happens.

    THE PREFIX EQUALITY IS THE LOAD-BEARING HALF. Staging is only durable because
    `lineage/api/reconcile_cron.py` drains `LINEAGE_OUTBOX_URI` and re-publishes what it finds; a catalog
    staging anywhere else would write objects that nothing on the estate ever reads, which looks exactly
    like durability and is not.
    """
    docs = _rendered_docs()
    catalog_env = _env_by_component(docs, "catalog")
    lineage_env = _env_by_component(docs, "lineage")
    assert catalog_env, "no catalog Deployment rendered"

    staged = catalog_env.get("LANCE_LINEAGE_OUTBOX_URI", "")
    assert staged, (
        "the catalog Deployment renders no LANCE_LINEAGE_OUTBOX_URI, so `publish_lineage_with_outbox` "
        "degrades to a plain publish: a crash between the Lance commit and the publish loses the write "
        "announcement, and with it the bronze->silver->gold run that announcement triggers"
    )
    drained = lineage_env.get("LINEAGE_OUTBOX_URI", "")
    assert staged == drained, (
        f"the catalog stages to {staged!r} but the lineage relay drains {drained!r} — staged events would "
        "sit in a prefix nothing re-ingests or re-publishes, which is indistinguishable from durability "
        "until an outage"
    )

    # The pair is ONE mechanism behind ONE switch: staging with the drain off leaves objects nothing
    # collects, so an operator turning the chain off must turn off both halves, not one.
    off = _env_by_component(_rendered_docs("services.lineage.outbox.enabled=false"), "catalog")
    assert "LANCE_LINEAGE_OUTBOX_URI" not in off, "services.lineage.outbox.enabled=false must also stop the catalog staging (the drain is off with it)"
