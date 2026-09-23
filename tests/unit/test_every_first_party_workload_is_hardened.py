"""Hardening covers every first-party workload we TEMPLATE — Deployments, StatefulSets, Jobs, and the
init containers inside them.

[[XC-061]]. `test_every_first_party_container_carries_the_HARDENING_the_chart_claims` walks Deployments
and their `containers` only, so three kinds of workload were never asked:

- **Jobs and StatefulSets.** Six Jobs and the AGE StatefulSet render with none of the baseline. A Job
  runs with the same reach as a Deployment and several of these hold credentials — the OpenBao seed and
  the MinIO user-provisioning Jobs by definition.
- **initContainers.** `rask-lineage` passes the existing gate on its app container while its `wait-age`
  init container carries nothing. An init container shares the pod and runs FIRST.
- **Workloads the exemption list calls subcharts and we actually template.** `_UNCOVERED_DEPLOYMENTS`
  lumps "not ours to template" together with OpenBao, Dex and MinIO — all rendered from
  `rask/templates/`. The row's title names the first two as the ones that most need it: the secret
  store and the IdP.

DERIVED FROM THE RENDER SOURCE, not from a name list. Every doc rendered from `rask/templates/` is ours;
anything under `rask/charts/` belongs to a subchart. A hand-written tuple is how the gate this widens
skipped four fleet Deployments silently, and the same tuple is why OpenBao reads as a subchart it is not.

MEASURED 2026-09-23 under default values: **12 first-party workloads, 13 containers** carry none of the
baseline. The row says "7 first-party containers and 3 Jobs", which undercounts.
"""

from __future__ import annotations

import re

import pytest
import yaml
from test_invariants import _helm_template


#: The baseline `lance.securityContext` writes. A container missing EVERY one of these was never
#: hardened at all — the condition this gate is about, as distinct from one missing a single key.
BASELINE = ("runAsNonRoot", "seccompProfile", "allowPrivilegeEscalation", "readOnlyRootFilesystem")

#: Workloads that genuinely are not ours to template, keyed on the render SOURCE rather than the name.
#: Nothing belongs here that lives under `rask/templates/`.
_SUBCHART_SOURCE = "rask/charts/"

WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")

#: Both container slots, named once so the ratchet cannot be narrowed by editing a parametrize list.
#: Dropping `initContainers` from that list shrinks the gate's reach and every test still passes —
#: caught by mutation, which is exactly the failure this constant exists to make impossible.
SLOTS = ("containers", "initContainers")


def _first_party_workloads() -> list[tuple[str, str, dict]]:
    """(kind, name, podSpec) for every workload rendered from this chart's OWN templates."""
    rendered = _helm_template()
    out: list[tuple[str, str, dict]] = []
    for chunk in rendered.split("\n---\n"):
        source = re.search(r"^# Source: (\S+)", chunk, re.MULTILINE)
        if not source or _SUBCHART_SOURCE in source.group(1):
            continue
        try:
            doc = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict) or doc.get("kind") not in WORKLOAD_KINDS:
            continue
        spec = doc.get("spec") or {}
        template = (spec.get("jobTemplate") or {}).get("spec", {}).get("template") if doc["kind"] == "CronJob" else spec.get("template")
        pod = (template or {}).get("spec") or {}
        out.append((doc["kind"], doc["metadata"]["name"], pod))
    return out


def test_the_walk_sees_the_chart() -> None:
    """Without this every assertion below passes by rendering nothing."""
    workloads = _first_party_workloads()
    assert len(workloads) >= 20, f"only {len(workloads)} first-party workloads rendered — the walk or the render is broken"
    assert any(kind == "Job" for kind, _, _ in workloads), "no Jobs rendered; this gate exists because Jobs were never walked"
    assert set(SLOTS) == {"containers", "initContainers"}, "the ratchet stopped walking a container slot"
    assert any(pod.get("initContainers") for _, _, pod in workloads), "no init containers rendered; one of them is why this gate is parametrised"


#: The containers that render without the full baseline TODAY, measured 2026-09-23 under default values.
#:
#: A RATCHET, NOT AN EXEMPTION LIST, and the difference is the whole design: a container may be here only
#: because it is already unhardened, never because someone decided it should be. Nothing may be ADDED —
#: a new unhardened container reds this gate — and every line removed is a container hardened for good.
#:
#: WHY IT IS NOT SIMPLY FIXED IN ONE GO. Most of these run THIRD-PARTY IMAGES the chart templates itself
#: (postgres, minio, openbao, dex, the mc/nats/kubectl CLIs), where `readOnlyRootFilesystem` is the key
#: that breaks things: each writes somewhere under its own root unless given a mount. Applying the
#: baseline blind would trade a hardening gap for a crash loop, so each comes off this list with its pod
#: observed running, which is a per-workload piece of work rather than one edit.
_UNHARDENED_TODAY: dict[str, tuple[str, ...]] = {
    # Ours to fix, and first in line: short-lived Jobs and init containers running our own or a CLI image.
    "Job/openfga-migrate/migrate": ("containers",),
    "Job/openbao-seed/seed": ("containers",),
    "Job/dapr-inject-sweep/sweep": ("containers",),
    "Job/nats-stream/nats": ("containers",),
    "Job/minio-mkbucket/mc": ("containers",),
    "Job/minio-scoped-users/mc": ("containers",),
    "Job/openfga-model/write-model": ("containers",),
    "Job/greptimedb-ttl/set-ttl": ("containers",),
    "Job/kueue-setup/apply-queues": ("containers",),
    "Job/kueue-setup/restart-kueue-controller": ("init",),
    "Job/kueue-setup/await-kueue-controller": ("init",),
    # Stateful third-party images: each needs a writable path before the baseline can land.
    "StatefulSet/age/postgres": ("containers",),
    "StatefulSet/minio/minio": ("containers",),
    "Deployment/openbao/openbao": ("containers",),
    "Deployment/dex/dex": ("containers",),
    # Infra we template, gated behind `security.infraContexts.enabled` (values.yaml defaults it OFF and
    # stages the flip explicitly) — a sequenced decision, not an oversight.
    "Deployment/otel-collector/otel-collector": ("containers",),
    "Deployment/dapr-dashboard/dashboard": ("containers",),
}


def _key(kind: str, name: str, container: str) -> str:
    """`Kind/workload/container`, with the release prefix stripped so the key survives a rename."""
    for prefix in ("release-name-", "rask-"):
        name = name.removeprefix(prefix)
    return f"{kind}/{re.sub(r'-r\d+$', '', name)}/{container}"


@pytest.mark.parametrize("slot", SLOTS)
def test_no_NEW_first_party_container_renders_without_the_baseline(slot: str) -> None:
    """The ratchet. Parametrised over the SLOT, because walking only `containers` is how an init hid.

    `rask-lineage` passes the older Deployments-only gate on its app container while its `wait-age` init
    container carries nothing — an init container shares the pod and runs FIRST.
    """
    tag = "init" if slot == "initContainers" else "containers"
    unexpected: dict[str, list[str]] = {}
    for kind, name, pod in _first_party_workloads():
        for container in pod.get(slot) or []:
            missing = [key for key in BASELINE if key not in (container.get("securityContext") or {})]
            if not missing:
                continue
            key = _key(kind, name, container["name"])
            if tag not in _UNHARDENED_TODAY.get(key, ()):
                unexpected[key] = missing

    assert not unexpected, (
        f"NEW first-party {slot} rendering without the baseline: {unexpected}. "
        "Harden it, or argue for it by name in `_UNHARDENED_TODAY` — the list only shrinks."
    )


def test_the_ratchet_names_nothing_that_is_already_hardened() -> None:
    """A stale entry is how a ratchet stops ratcheting: it would excuse a REGRESSION on that container."""
    rendered: dict[str, set[str]] = {}
    for kind, name, pod in _first_party_workloads():
        for slot, tag in (("containers", "containers"), ("initContainers", "init")):
            for container in pod.get(slot) or []:
                if [key for key in BASELINE if key not in (container.get("securityContext") or {})]:
                    rendered.setdefault(_key(kind, name, container["name"]), set()).add(tag)

    stale = {key: sorted(tags) for key, tags in _UNHARDENED_TODAY.items() if set(tags) - rendered.get(key, set())}
    assert not stale, f"the ratchet names containers that now render the baseline — remove them: {stale}"
