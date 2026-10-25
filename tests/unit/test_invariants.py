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
import json
import re
import shlex
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit import chart_render
from tests.unit.chart_render import PREREQ_ARGS, RAY_ARGS


REPO = Path(__file__).resolve().parents[2]


CHART = REPO / "chart"


def _helm_template(*set_values: str) -> str:
    """Render the chart, skipping the test if helm is not on PATH or in .localbin."""
    import shutil

    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [helm, "template", str(CHART)]
    # Side-loaded images: see `rask.image` in _helpers.tpl — the chart refuses a bare
    # `<component>:<tag>` unless this is set, because that is docker.io and not a local image.
    argv += ["--set", "image.localImages=true"]
    # The identity values every render needs since auth defaults ON (2026-08-06), and the ESO APIs the
    # chart requires ([[XC-004]]). The chart refuses OIDC without an issuer ON PURPOSE — that refusal
    # is what stops a forgotten values file installing an ungoverned estate. Supplying dev values HERE keeps
    # every other render test testing its own subject rather than re-testing the guard.
    argv += [*PREREQ_ARGS, *RAY_ARGS]
    argv += ["--set", "auth.oidc.issuer=http://localhost:8080/dex"]
    argv += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    for value in set_values:
        argv += ["--set", value]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def test_no_SCOPED_storage_identity_is_published_with_the_ROOT_secret() -> None:
    """CONTRACT (security, standing rule): a scoped storage identity's secret is DERIVED, never the root.

    The estate mints scoped users and publishes each one's secret into OpenBao for ESO to sync. Four
    derived it from the root; `ray-compute` alone read
    `rayComputeSecretKey | default objectStore.secretKey` — and `rayComputeSecretKey` ships empty, so the
    published value WAS the RustFS root credential.

    MEASURED on the live estate 2026-09-11, by hash so nothing was disclosed: `ray-compute-secret-key`,
    `ray-compute-access-key` and `object-store-secret-key` in `rask-infra-credentials` were byte-identical,
    all three 11 bytes. The chart CREATES `rask-ray-compute` with the derived secret
    (`object-store-scoped-users.yaml`) and PUBLISHED the root one under its name, so the pair can never
    match: a consumer either signs as root — defeating the scoping entirely — or fails
    `SignatureDoesNotMatch`. The Ray head's own manifest asserts the opposite in a comment, which is
    how it survived.

    ASSERTED ON THE RENDER: the root is never in it ([[XC-004]]), so the dev OpenBao's seed writes each
    field from a file, and a scoped field must name a file `scoped_secret` derived (for an identity, or for
    a data base's credential reference), never the root's (`$G/object-store-secret-key`). Generic over every
    scoped field the seed writes, under either spelling (`secret_key`, `<x>-secret-key`).
    """
    rendered = _helm_template("auth.bootstrapAdmin=dex~gate-probe")

    fields = re.findall(r"\b((?:[a-z]+-)*secret[-_]key)=@\"([^\"]+)\"", rendered)
    fields = [(field, source) for field, source in fields if field != "object-store-secret-key"]
    assert fields, "the seed writes no scoped secret field, so this gate would pass vacuously"
    offenders = [f"{field}={source}" for field, source in fields if not source.startswith(("$HOME/seed/scoped-", "$HOME/seed/base-"))]
    assert offenders == [], "a SCOPED identity is published with the ROOT storage secret:\n  " + "\n  ".join(offenders)


def test_every_first_party_deployment_is_hardened() -> None:
    """The docs claim "every Deployment has probes + preStop". The gateway had NEITHER (audit 2026-07-14).

    An "every" claim in prose is worth nothing; this loop is what makes it true. It renders the chart and
    checks each FIRST-PARTY Deployment (third-party subcharts — dapr/nats/openfga/dex — are not ours to
    template). preStop matters most on the gateway: it is the INGRESS, so without a drain delay a rolling
    update drops in-flight requests while kube-proxy is still routing to the terminating pod.

    IT NO LONGER NAMES ITS OWN SUBJECTS. This carried a hand-written tuple of ten name fragments —
    gateway, catalog, lineage, compaction, web, notifications —
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
    for doc in _first_party_deployments(_rendered_docs()):
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


def _hook_buckets(rendered: str) -> list[str]:
    """The buckets the scoped-users hook's provisioning step is told to create, in order (`rustfs_admin.py --bucket`).

    The step makes each one on the store it provisions (the `rustfs-provisioning` lane runs it against a real RustFS),
    so what is left to hold offline is the render: which names reach it.
    """
    job = _job_by_component(rendered, "object-store-scoped-users")
    assert job is not None, "the scoped-users hook does not render"
    script = yaml.safe_load(job)["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    argv = shlex.split(script[script.index("exec python /srv/rustfs_admin.py") :].replace("\\\n", " "))
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--bucket"]


def test_the_hook_creates_every_platform_bucket_and_every_zone() -> None:
    """Every bucket a value names reaches the step that creates it.

    Each source is its own hop in the template, and a hop dropped is invisible while another value names
    the same bucket. So the root, the platform bucket and the zone here are each named by one value only.
    """
    rendered = _helm_template(
        "singleTenant.enabled=true",
        "objectStore.bucket=root-x",
        "objectStore.buckets={extra-platform}",
        "medallion.buckets.gold=acme-gold",
    )

    missing = {"root-x", "extra-platform", "acme-gold"} - set(_hook_buckets(rendered))

    assert not missing, f"the hook is told to create none of these buckets: {sorted(missing)}"


def test_a_renamed_bucket_leaves_nothing_behind_under_its_old_name() -> None:
    """Rename the root: the hook makes it under its new name only. A default spelled a second time in the values
    shows up here as a bucket the hook makes under the old name, which nothing uses.

    With no platform bucket declared the platform set is empty, which is the render that hands the hook an empty
    name unless the template drops it.
    """
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    spellings = {"root-x", values["objectStore"]["bucket"]}
    rendered = _helm_template("singleTenant.enabled=true", "objectStore.bucket=root-x", "objectStore.buckets=null")

    buckets = _hook_buckets(rendered)

    assert set(buckets) & spellings == {"root-x"}, f"the hook makes {sorted(set(buckets) & spellings)} of {sorted(spellings)}, wanted ['root-x']"
    assert "" not in buckets and len(buckets) == len(set(buckets)), f"the hook is told to create {buckets}"


def test_every_dapr_annotated_pod_carries_the_injector_webhook_label() -> None:
    """Fail-closed sidecar injection is only safe if the two markers stay in lockstep.

    Dapr's injector is a mutating webhook shipping `failurePolicy: Ignore`: a pod created while the injector is
    unreachable is SILENTLY admitted with no daprd container, and nothing recreates it (a CrashLoopBackOff restarts
    the container inside the same pod), so every governed app dies on "secret unavailable from Dapr store … failing
    closed". The Dapr control plane is a prerequisite ([[XC-128]]) whose injector fails closed, scoped by an
    `objectSelector` to the `dapr.io/enabled` LABEL (deploy/fixtures/dapr; read back on the live webhook), and rask's
    chart stamps that label. An annotation without the label is never sent to the webhook, so the pod is silently
    un-injected again: every pod template carrying the annotation must carry the label.
    """
    import yaml

    rendered = _helm_template("singleTenant.enabled=true")
    docs = [d for d in yaml.load_all(rendered, Loader=FAST_LOADER) if d]

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
#: real deployment, so it points at the platform's store rather than the dev OpenBao and names the host
#: its routes answer. That keeps a test about IMAGE PINNING testing its own subject.
_REAL_REGISTRY = (
    "image.localImages=false",
    "image.repository=reg.example",
    "route.hostnames[0]=lance.example.com",
    "openbao.enabled=false",
    f"openbao.externalAddr={chart_render.SECRET_STORE_URL}",
    "dex.enabled=false",
    "auth.oidc.issuer=https://auth.example.com/dex",
    "auth.oidc.connectors.ldap=corp",
    "signing.provisioned=true",
    "nats.auth.provisioned=true",
    "objectStore.enabled=false",
    "objectStore.externalEndpoint=https://s3.example.com",
)


def test_a_per_component_pin_beats_the_global_tag_and_a_digest_beats_the_pin() -> None:
    """#135 — the chart must be able to describe a fleet that is NOT one tag.

    One `image.tag` could not: a live estate ran a catalog tag across 11 services, a different tag on
    flows/notifications, a third across the 7 zones, and an ingest DIGEST. Rendering all of
    that from one value is what made `helm upgrade` destructive — every image rewritten to a tag the
    node did not hold, whole fleet ImagePullBackOff, recovered by hand. Observed twice on 2026-08-06.

    Precedence is asserted end to end, because a pin that is merely ACCEPTED and then overridden is
    worse than no pin: it reads as safety while the deploy still rewrites the image.
    """
    pinned = _helm_template(*_REAL_REGISTRY, "image.tags.notifications=PINNED")
    assert 'image: "reg.example/notifications:PINNED"' in pinned, "a per-component tag did not reach the render"
    assert 'image: "reg.example/lance-rest-catalog:dev"' in pinned, "the pin leaked onto a component that did not ask for it"

    # A digest is a CONTENT pin — a reconciler's, typically — so no tag may undo it.
    both = _helm_template(*_REAL_REGISTRY, "image.tags.notifications=IGNORED", "image.digests.notifications=sha256:abc")
    assert 'image: "reg.example/notifications@sha256:abc"' in both, "a per-component tag overrode a digest"


@pytest.mark.parametrize("key", ["image.tags.lance-rest-catalog", "image.digests.notifications"])
def test_an_image_reference_that_helm_parsed_as_a_number_is_refused_at_render(key: str) -> None:
    """A short sha can be all digits, and `--set` parses `93951597` as an int64.

    The image helper formats it with `%s`, so the render carried `lance-rest-catalog:%!s(int64=93951597)`; measured
    2026-10-14 on rev 3, the pre-upgrade hook's pod sat in InvalidImageName for the whole `--wait` and the upgrade
    failed. A number is also lossy (`--set` turns `01234567` into 1234567), so the schema refuses it and names the
    key, and `--set-string` is the way to pass one.
    """
    with pytest.raises(subprocess.CalledProcessError) as refused:
        _helm_template(*_REAL_REGISTRY, f"{key}=93951597")
    assert key.rsplit(".", 1)[-1] in refused.value.stderr and "string" in refused.value.stderr, refused.value.stderr


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
    # rule is only as wide as the values it is rendered with, so the lane that is a deployment
    # choice is switched on here rather than inherited: maintenance's write-event lane.
    rendered = _helm_template(
        "dapr.enabled=true",
        "dapr.resiliency.enabled=true",
        "maintenance.arrivalEnabled=true",
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


def test_the_object_store_carries_NO_credential_in_its_environment() -> None:
    """The store pod reads every credential from a mounted file; its environment carries none.

    The root pair signs every request as root and every STS session token the store issues, so it is the
    estate's storage trust root. RustFS reads it from `RUSTFS_ACCESS_KEY_FILE` / `RUSTFS_SECRET_KEY_FILE`
    (`.claude/skills/rask-rustfs/references/config.md`), so no env var need hold it: an env value or a
    `secretKeyRef` is readable by anything that can read the process environment, a file in a 0400 secret
    volume is not. The OIDC client secret has no `_FILE` variant and web-identity verification needs only the
    client id (`references/security.md` § Web identity), so the store is given none at all.

    The files must also RESOLVE: a secret volume naming a key nothing writes is a pod that never starts.
    """
    docs = list(chart_render.render(*chart_render.DEFAULT_ARGS))
    written = _written_secrets(docs)
    stores = [d for d in chart_render.render_fixture("rustfs") if d.get("kind") == "StatefulSet"]
    assert stores, "the object store did not render — this gate would pass vacuously"

    for store in stores:
        pod = store["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in pod.get("volumes") or []}
        for container in [*pod.get("initContainers", []), *pod["containers"]]:
            env = container.get("env") or []
            for entry in env:
                assert "valueFrom" not in entry, f"{entry['name']} reaches the store's environment from a reference: {entry!r}"
                name = str(entry["name"])
                if not name.endswith("_FILE"):
                    assert not re.search(r"(SECRET|ACCESS_KEY|PASSWORD|TOKEN)", name), f"{name} carries a credential in the store's env"

            files = {e["name"]: str(e.get("value", "")) for e in env if e["name"] in ("RUSTFS_ACCESS_KEY_FILE", "RUSTFS_SECRET_KEY_FILE")}
            assert set(files) == {"RUSTFS_ACCESS_KEY_FILE", "RUSTFS_SECRET_KEY_FILE"}, f"the store reads its root pair from {sorted(files)}"
            for var, path in files.items():
                mount = next((m for m in container.get("volumeMounts") or [] if path.startswith(m["mountPath"].rstrip("/") + "/")), None)
                assert mount is not None, f"{var}={path} is under no mount of the store container"
                secret = (volumes.get(mount["name"]) or {}).get("secret")
                assert secret, f"{var}={path} is mounted from {mount['name']!r}, not a secret volume"
                assert secret.get("optional") is not True, f"{var}'s secret volume is optional: the store would boot on its default root pair"
                filename = path.removeprefix(mount["mountPath"].rstrip("/") + "/")
                keys = [i["key"] for i in secret.get("items") or [] if i["path"] == filename]
                assert keys, f"{var}={path} maps to no key of Secret {secret['secretName']!r}"
                held = written.get(secret["secretName"])
                assert held is not None, f"{var} reads Secret {secret['secretName']!r}, which nothing rask's chart renders writes"
                assert keys[0] in held, f"{var} reads key {keys[0]!r}, absent from Secret {secret['secretName']!r} (has {sorted(held)})"


def test_the_notifications_pod_asks_for_a_sidecar_and_is_allowed_to_receive_one() -> None:
    """The sidecar's TWO halves on the one Deployment that cannot work without it.

    `test_every_dapr_annotated_pod_carries_the_injector_webhook_label` proves the correspondence for
    every pod that asks — and says nothing at all about a pod that never asks. That is the vacuous case
    this closes: the whole notification plane is actor state plus one bus subscription, so a
    Deployment rendered with no `dapr.io/*` annotations would come up healthy, serve its health surface
    and its route, and fail every inbox route forever — on a pod whose probes stay green,
    because actor registration is process-local and cannot notice that no sidecar was injected.

    The app-id is asserted because it is not decoration: `lance-statestore`'s `scopes`, the
    `lineage-pubsub-notifications` component's subscriber list and the resiliency CRDs all key on that
    exact string, and a mismatch disables actor hosting with no error anywhere. The reconciler reads
    lineage's feed through this sidecar and carries no retry of its own, so the invocation Resiliency
    targeting `lineage` must be scoped to this app-id: a Resiliency applies only in the sidecars its
    `scopes` name.
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

    feed_retry = [
        doc
        for doc in yaml.load_all(rendered, Loader=FAST_LOADER)
        if isinstance(doc, dict) and doc.get("kind") == "Resiliency" and "lineage" in (((doc.get("spec") or {}).get("targets") or {}).get("apps") or {})
    ]
    assert [doc["scopes"] for doc in feed_retry] == [["notifications"]], "no Resiliency the notifications sidecar loads retries its lineage feed read"


# --------------------------------------------------------------------------------------------------
# 12. The notification plane's WIRING — the four facts that live in the chart and nowhere else
#
# The service's own suites prove what each route answers; none of them can see whether anything ever
# reaches those routes. Each guard below covers one address that is written down in exactly one place,
# read somewhere else, and whose omission produces a HEALTHY pod: a subscription on a component that does not exist, an actor
# host with no state store, and a kubelet probing a path the app does not serve. Two of the three have already shipped once in this estate on
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


def _fleet_config(docs: list[dict]) -> dict[str, str]:
    """The fleet ConfigMap — the one carrying `RASK_API_PREFIX` and the notifications plane's addresses."""
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and "RASK_API_PREFIX" in (doc.get("data") or {}):
            return doc["data"]
    raise AssertionError("no fleet ConfigMap rendered — every fleet address would fall back to a localhost default")


def _notifications_container(docs: list[dict]) -> dict:
    for doc in docs:
        if doc.get("kind") == "Deployment" and "notifications" in ((doc.get("metadata") or {}).get("name") or ""):
            return doc["spec"]["template"]["spec"]["containers"][0]
    raise AssertionError("no notifications Deployment rendered")


def test_the_inbox_subscribes_on_a_component_the_chart_actually_renders() -> None:
    """A subscription names its pubsub component by string, and a name that resolves to nothing is a
    STARTUP error the pod survives: daprd logs it, the app serves its health surface and its route,
    and not one lineage event is ever delivered.

    Both ends are asserted because they fail in different deployments. The chart's value is what the
    pod reads; the app's DEFAULT is what a dev run without the ConfigMap reads, and the two drifting
    apart is how a subscription works in one environment and is silently dead in the other: the name is
    a coordinate, and nothing else compares its two ends.
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


_LOCAL_INPUTS = ("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml"))
_EXTERNAL_RAY = "https://ray.example.com:8265"


@pytest.mark.parametrize(
    "overlay",
    [
        pytest.param(("--set", "image.localImages=true"), id="default-names-no-ray"),
        pytest.param(chart_render.PROD_ARGS, id="prod-names-no-ray"),
        pytest.param((*chart_render.PROD_ARGS, "--set-string", f"ray.dashboardUrl={_EXTERNAL_RAY}"), id="prod-external-ray"),
        pytest.param(_LOCAL_INPUTS, id="local-fixture-head"),
    ],
)
def test_every_ray_consumer_names_one_cluster_or_the_render_refuses(overlay: tuple[str, ...]) -> None:
    """[[CP-041]]: every Ray consumer names the one cluster the estate names, and rask installs none of it.

    A second key naming the Ray plane once split it: compute pruned job history on one head while other pods
    submitted to a head Service nothing rendered, and a head grown to 81,155 jobs OOMKilled compute that way.
    Every consumer in the chart renders `rask.rayDashboardUrl` (the zones' Serve origin; compute and the annotator are
    their own releases and are given the address as a value), so no pod names a Ray address under a key of its own,
    whether the estate names a Ray or not.

    The address reaches its readers alone (CP-056): a shared ConfigMap would hand a Ray address to every fleet pod that
    loads it, the lakehouse's controlplane and notifications among them, which never call Ray.

    The engine is bring-your-own ([[XC-125]], ADR 0153): no render holds a `ray.io` object or the KubeRay operator, so
    the address is always one the estate names (the local overlay names the ray-cluster fixture's head Service).
    """
    text = chart_render.render_chart_text(CHART, *overlay, base=chart_render.UNNAMED_RAY_ARGS)
    docs = [doc for doc in yaml.load_all(text, Loader=FAST_LOADER) if isinstance(doc, dict)]
    named: dict[str, set[str]] = {}
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and "RAY_DASHBOARD_URL" in (doc.get("data") or {}):
            named.setdefault(f"ConfigMap/{doc['metadata']['name']} RAY_DASHBOARD_URL", set()).add(doc["data"]["RAY_DASHBOARD_URL"])
    for workload, _, container in chart_render.containers(tuple(docs)):
        for name, value in chart_render.env_of(container).items():
            if name in ("RAY_DASHBOARD_URL", "MEDIA_SERVE_DISCOVERY_URL") or "RAY_ADDRESS" in name:
                named.setdefault(f"{workload} {name}", set()).add(value)

    assert named == {}, f"a pod names a Ray address under a key of its own: {named}"
    engine = sorted(
        f"{doc['kind']}/{doc['metadata']['name']}"
        for doc in docs
        if str(doc.get("apiVersion", "")).startswith("ray.io/") or "kuberay" in str((doc.get("metadata") or {}).get("labels") or {}).lower()
    )
    assert not engine, f"the render installs part of a Ray engine, which is the engine owner's: {engine}"


def test_every_bus_client_dials_the_bus_the_estate_names_and_the_render_runs_none() -> None:
    """[[XC-129]]: NATS JetStream is a prerequisite (ADR 0153). The platform runs the bus and rask names it in `nats.url`;
    every pub/sub Component and the stream Job dial that one address, nothing else reads a NATS address, and no render holds a NATS
    server or its JetStream volumes (nats-box's CLI, which the stream Job and the dev mint run, is a client). That the
    render refuses with no bus named is the prerequisite gate in test_no_credential_is_carried_by_the_render.py."""
    docs = chart_render.render(*chart_render.DEFAULT_ARGS, "--set", "networkPolicy.enabled=true")
    dialled: dict[str, set[str]] = {}
    for doc in docs:
        if doc.get("kind") == "Component" and (doc.get("spec") or {}).get("type") == "pubsub.jetstream":
            for row in doc["spec"]["metadata"]:
                if row["name"] == "natsURL":
                    dialled.setdefault("pubsub.jetstream", set()).add(row["value"])
    for workload, _, container in chart_render.containers(docs):
        if workload.startswith("Job/") and "-nats-stream-" in workload:
            dialled.setdefault("stream Job", set()).update(re.findall(r'^\s*S="([^"]+)"', container["command"][-1], re.MULTILINE))
        if url := chart_render.env_of(container).get("RASK_NATS_URL"):
            dialled.setdefault("RASK_NATS_URL", set()).add(url)
    assert dialled == {client: {chart_render.NATS_URL} for client in ("pubsub.jetstream", "stream Job")}, dialled

    bus = sorted(
        f"{workload}/{name} ({container['image']})"
        for workload, name, container in chart_render.containers(docs)
        if container["image"].split("/")[-1].split(":")[0] in {"nats", "nats-server-config-reloader"}
    )
    bus += sorted(
        f"{doc['kind']}/{doc['metadata']['name']}"
        for doc in docs
        if doc.get("kind") in {"PersistentVolumeClaim", "StatefulSet"} and "nats" in doc["metadata"]["name"]
    )
    assert not bus, f"the render runs part of a NATS bus, which is the platform's: {bus}"


@pytest.mark.parametrize("stand_in", ["dex", "rustfs", "openbao"])
def test_the_render_runs_no_development_stand_in(stand_in: str) -> None:
    """[[XC-131]]: the development stand-ins are fixtures (ADR 0153), so a render with every gate that names one on
    (`dex.enabled`, `objectStore.enabled`, `openbao.enabled`) runs no pod of it and serves none; rask reaches each by its
    endpoint."""
    docs = chart_render.render(*chart_render.DEFAULT_ARGS, "--set", "auth.enabled=true")
    found = [
        f"{doc['kind']}/{doc['metadata']['name']}"
        for doc in docs
        if (doc.get("kind") in {"Deployment", "StatefulSet"} and doc["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component") == stand_in)
        or (doc.get("kind") == "Service" and (doc["spec"].get("selector") or {}).get("app.kubernetes.io/component") == stand_in)
    ]
    assert not found, f"the render runs the development {stand_in}, which is a fixture: {found}"


def test_a_production_render_runs_no_store_the_platform_serves_and_dials_the_platforms() -> None:
    """[[XC-132]], [[XC-149]]: OpenFGA, the Postgres behind OpenFGA and the Dapr state store, and the secret store are the
    platform's in production, which names each (ADR 0153). The production render runs no workload or identity of any of
    them and holds no object of the secret store. Every FGA client, Postgres client and secret reader dials the named
    endpoint, the pods whose sidecar reads the store are the pods admitted to it and no others, and no credential rides
    along in an env value: the FGA token is a projected file and the Postgres password the store's `postgres-password`,
    both by reference."""
    fga, pg, store = "https://fga.platform.example", "pg.platform.example", "https://vault.platform.example:8200"
    docs = chart_render.render(
        *chart_render.PROD_ARGS,
        "--set-string", f"auth.fgaApiUrl={fga}", "--set-string", f"age.externalHost={pg}", "--set-string", f"openbao.externalAddr={store}",
    )  # fmt: skip

    found = sorted(
        f"{workload}/{name} ({container['image']})"
        for workload, name, container in chart_render.containers(docs)
        if container["image"].split("/")[-1].split(":")[0] in {"openfga", "age", "postgres", "openbao"}
    )
    found += sorted(
        f"{doc['kind']}/{doc['metadata']['name']}"
        for doc in docs
        if doc["metadata"]["name"] in {"rask-age", "rask-openfga", "rask-sa-age"} or "openbao" in doc["metadata"]["name"]
    )
    assert not found, f"the render runs part of a store the platform serves: {found}"

    text = json.dumps(docs)
    in_chart = r"rask-(?:age|openfga|openbao)(?::\d| \d|\.[a-z0-9-]+\.svc)|(?:@|//|host=|-h )rask-(?:age|openfga|openbao)\b|'rask-(?:age|openfga|openbao)'"
    assert not re.findall(in_chart, text), "a client still dials the in-chart instance"
    fga_urls = {env[k] for _, _, c in chart_render.containers(docs) for k in ("RASK_FGA_API_URL", "FGA_API_URL") if k in (env := chart_render.env_of(c))}
    assert fga_urls == {fga}, fga_urls

    [secrets] = [doc for doc in docs if doc.get("kind") == "Component" and doc["spec"]["type"].startswith("secretstores.")]
    named = {row["value"] for row in secrets["spec"]["metadata"] if row["name"] == "vaultAddr"}
    named |= {doc["spec"]["provider"]["vault"]["server"] for doc in docs if doc.get("kind") == "SecretStore"}
    assert named == {store}, f"the Dapr secret store and ESO's SecretStore read {named}, not the store the deployment names"
    peer = {"ipBlock": {"cidr": chart_render.SECRET_STORE_CIDR}}
    admitting = [
        doc["spec"]["podSelector"]
        for doc in docs
        if doc.get("kind") == "NetworkPolicy" and any(peer in (rule.get("to") or []) for rule in doc["spec"].get("egress") or [])
    ]
    pods = {f"{doc['kind']}/{doc['metadata']['name']}": doc["spec"]["template"] for doc in docs if doc.get("kind") in {"Deployment", "StatefulSet"}}
    readers = {name for name, pod in pods.items() if (pod["metadata"].get("annotations") or {}).get("dapr.io/app-id") in secrets["scopes"]}
    admitted = {name for name, pod in pods.items() if any(chart_render.selects(selector, pod["metadata"]["labels"]) for selector in admitting)}
    assert "Deployment/rask-catalog" in readers, f"the readers read as {sorted(readers)}"
    assert admitted == readers, (
        f"admitted to the store and not reading it: {sorted(admitted - readers)}; reading it and not admitted: {sorted(readers - admitted)}"
    )

    leaked = sorted(
        f"{workload}/{name}: {key}"
        for workload, name, container in chart_render.containers(docs)
        for key, value in chart_render.env_of(container).items()
        if isinstance(value, str) and "://" in value and urllib.parse.urlsplit(value).password
    )
    assert not leaked, f"a credential rides in an env value rather than by reference: {leaked}"


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


def test_the_sidecars_non_telemetry_config_survives_telemetry_being_off() -> None:
    """A sidecar may reference exactly ONE `dapr.io/config`, so everything per-sidecar lives in one
    object, and only its tracing stanza is about telemetry. Metric cardinality and api-logging bound the
    sidecar's `:9090` exposition and its log stream, which both exist whether or not this estate ships a
    collector, so a bound that vanishes with telemetry is not a bound.

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
    "maintenance": "MAINTENANCE_DOCS",
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
    off = _app_containers(_rendered_docs())
    missing = sorted(set(_DOCS_ENV_BY_WORKLOAD) - set(off))
    assert not missing, (
        f"these workloads render no docs env var at all: {missing} — a service whose docs flag no manifest sets is one whose default nobody chose"
    )
    for workload, container in off.items():
        var = _DOCS_ENV_BY_WORKLOAD[workload]
        assert _env_of(container)[var].lower() in {"false", "0"}, f"{workload} renders {var}={_env_of(container)[var]} by default — docs are opt-in"

    on = _app_containers(_rendered_docs("docs.enabled=true"))
    for workload in _DOCS_ENV_BY_WORKLOAD:
        var = _DOCS_ENV_BY_WORKLOAD[workload]
        value = _env_of(on[workload])[var]
        assert value.lower() in {"true", "1"}, f"docs.enabled=true left {workload} at {var}={value} — a knob that cannot open is a deletion"


#: Deployments this gate does NOT cover, enumerated from an actual render rather than guessed.
#: Matched as substrings of the rendered name, like the sibling gates in this file.
#:
#: Two different reasons, kept apart on purpose:
#:
#:  * SUBCHARTS — dapr, nats, openfga, dex, rustfs. Not ours to template,
#:    so not ours to harden.
#:  * INFRA PODS WE DO TEMPLATE — the OTel collector. Its securityContext is gated behind
#:    `security.infraContexts.enabled`, which values.yaml defaults OFF and stages explicitly:
#:    "both default OFF (behavior-identical); flip after the §7a live checks". That is a known,
#:    sequenced decision with its own live-check step, not an oversight, so this gate must not
#:    silently pre-empt it. It is listed HERE, visibly, rather than being missing from a
#:    hand-written first-party tuple where nobody could tell the difference.
_UNCOVERED_DEPLOYMENTS = (
    "dapr", "nats", "openfga", "dex", "rustfs",
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

    THE FAILURE THIS PINS. `dapr.enabled=false` is a documented, supported toggle, and
    `services.yaml`'s own fail message tells an operator to pair it with `catalog.controlEmit=false`.
    That pair rendered cleanly — and left THIRTEEN Deployments (all seven zones, maintenance, the
    producer, three stage runners and lineage) carrying a reference to `-dapr-app-token`, which is gated on
    `dapr.enabled` and therefore absent. Each fails with CreateContainerConfigError; nothing in the
    render says why.

    The cause was one Secret doing double duty under two different gates: the Dapr app token
    (`dapr.enabled`) and `LINEAGE_SERVICE_TOKEN`, the service credential for reading lineage
    (`auth.enabled`).

    Checked across the TOGGLE COMBINATIONS, not just the default render, because the default is the one
    configuration this class cannot appear in — every consumer and its Secret are on together there.
    """
    combinations = [
        ("default", []),
        ("sidecars off", ["dapr.enabled=false", "catalog.controlEmit=false"]),
        ("auth off", ["auth.enabled=false"]),
        ("both off", ["dapr.enabled=false", "catalog.controlEmit=false", "auth.enabled=false"]),
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
    write commits, and a pipeline reading the commit feed reacts to that announcement — so a lost
    publish does not merely under-report provenance, the downstream run silently never happens.

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
