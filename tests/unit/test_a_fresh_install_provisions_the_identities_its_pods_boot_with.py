"""On a FRESH install, every scoped storage identity a pod boots with exists before `--wait` needs it.

helm's install order is: apply the manifest, block on `--wait` until every resource is Ready, THEN run
`post-install` hooks. The scoped MinIO users (`rask-catalog`, `rask-lineage`, ...) were created only by
a hook whose install phase was `post-install`, while the catalog presents `rask-catalog` at boot and
fails fast when the store refuses it (`build_namespace` in `catalog/main.py` lists the root bucket).
So a first install could never finish: the catalog crash-looped for want of a user, `--wait` waited
for the catalog, and the hook that would create the user was queued behind the wait. The same
circularity `_helpers.tpl` "BOOTSTRAP JOBS" records for the OpenFGA migration, with the same remedy —
the provisioning runs IN the install wave, where `--wait` waits for it concurrently.

Upgrades keep the hook phases that file defends (`pre-upgrade` before the pods roll, `post-upgrade`
after a new bucket lands); `test_a_provisioned_identity_is_one_the_service_uses.py` pins those.
"""

from __future__ import annotations

import re

import pytest

from tests.unit import chart_render


def _install_render() -> tuple[dict, ...]:
    """`helm template` renders as an INSTALL (`.Release.IsInstall`), the first `make k3s-up`."""
    return chart_render.render(*chart_render.DEFAULT_ARGS)


def _root_key(docs: tuple[dict, ...]) -> str:
    """The tenant root access key, read off the bucket Job's own `mc alias set` line."""
    for doc in docs:
        if doc.get("kind") == "Job" and "mkbucket" in doc["metadata"]["name"]:
            script = doc["spec"]["template"]["spec"]["containers"][0]["command"][-1]
            match = re.search(r"mc alias set rfs \S+ (\S+) ", script)
            assert match, "the bucket Job no longer logs in with `mc alias set rfs <endpoint> <key>`"
            return match.group(1)
    raise AssertionError("no minio-mkbucket Job rendered")


def _booted_identities(docs: tuple[dict, ...]) -> set[str]:
    """Every S3 access key id a long-running workload presents, bar the root key."""
    root = _root_key(docs)
    found = set()
    for _workload, _name, container in chart_render.containers(docs):
        for name, value in chart_render.env_of(container).items():
            if name.endswith("S3_ACCESS_KEY_ID") and value and value != root:
                found.add(value)
    return found


def _provisioning_jobs(docs: tuple[dict, ...]) -> list[tuple[dict, str]]:
    return [
        (doc, doc["spec"]["template"]["spec"]["containers"][0]["command"][-1])
        for doc in docs
        if doc.get("kind") == "Job" and "mc admin user add" in str(doc["spec"]["template"]["spec"]["containers"][0].get("command"))
    ]


def test_the_install_render_presents_scoped_identities() -> None:
    """Guard: with none, every assertion below would pass on nothing."""
    assert {"rask-catalog", "rask-lineage"} <= _booted_identities(_install_render())


def test_every_identity_a_pod_boots_with_is_provisioned_IN_the_install_wave() -> None:
    docs = _install_render()
    in_wave = [(doc, script) for doc, script in _provisioning_jobs(docs) if "helm.sh/hook" not in (doc["metadata"].get("annotations") or {})]

    unprovisioned = sorted(
        identity for identity in _booted_identities(docs) if not any(f"mc admin user add rfs {identity} " in script for _, script in in_wave)
    )

    assert not unprovisioned, (
        f"on a fresh install these identities are presented at boot but provisioned only by a hook, which "
        f"helm runs AFTER `--wait` — and `--wait` cannot finish while the pods presenting them crash-loop: "
        f"{unprovisioned}. Provision them in a Job that is part of the install manifest."
    )


#: Overlays that drive every branch of `rask.minioCreatedBuckets`, which must mirror minio-mkbucket.
_BUCKET_OVERLAYS = {
    "default": (),
    "observability-off": ("--set", "observability.enabled=false"),
    "medallion-zones": ("--set", "medallion.buckets.ingest=zone-in", "--set", "medallion.buckets.gold=zone-gold"),
    "external-s3": ("--set", "minio.externalEndpoint=https://s3.example", "--set", "minio.buckets={ext-a,ext-b}"),
}


@pytest.mark.parametrize("overlay", list(_BUCKET_OVERLAYS), ids=list(_BUCKET_OVERLAYS))
def test_the_install_wave_waits_for_EXACTLY_the_buckets_the_bucket_job_creates(overlay: str) -> None:
    """The ordering the hook's weight gave it: a policy written before its bucket exists is accepted and
    governs nothing, so the in-wave copy waits for every bucket the bucket Job creates — and for no other,
    because a bucket nothing creates is a wait that never ends and an install that never finishes."""
    docs = chart_render.render(*chart_render.DEFAULT_ARGS, *_BUCKET_OVERLAYS[overlay])
    [mkbucket] = [doc for doc in docs if doc.get("kind") == "Job" and "mkbucket" in doc["metadata"]["name"]]
    created = set(re.findall(r"mc mb --ignore-existing rfs/(\S+)", mkbucket["spec"]["template"]["spec"]["containers"][0]["command"][-1]))
    in_wave = [(doc, script) for doc, script in _provisioning_jobs(docs) if "helm.sh/hook" not in (doc["metadata"].get("annotations") or {})]
    assert created and in_wave, f"{overlay}: nothing to compare — created={created} in-wave jobs={len(in_wave)}"

    for doc, script in in_wave:
        waited = set(re.findall(r"until mc ls rfs/(\S+) ", script[: script.index("mc admin policy create")]))
        assert waited == created, f"{overlay}: {doc['metadata']['name']} waits for {sorted(waited)} but the bucket Job creates {sorted(created)}"


#: `memory.peak` of one container running the Job's own sequence against MinIO, measured 2026-09-23 on
#: quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z: `mc alias set` 11 MB, `mc admin user add` 78 MB,
#: `mc admin policy attach` 145 MB. The admin API encrypts request bodies with Argon2id, which is what
#: `mc ls`/`mc mb` never pay — so the bucket Job fits in 128Mi and this one was OOMKilled at it, mid-way
#: through provisioning, on the first fresh install.
MC_ADMIN_PEAK_BYTES = 145_162_240


def test_the_provisioning_job_has_room_for_mc_admin() -> None:
    docs = _install_render()
    limits = {doc["metadata"]["name"]: doc["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] for doc, _ in _provisioning_jobs(docs)}
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30}

    starved = {name: limit for name, limit in limits.items() if int(limit[:-2]) * units[limit[-2:]] < 1.5 * MC_ADMIN_PEAK_BYTES}

    assert limits, "no provisioning Job rendered"
    assert not starved, f"these Jobs run `mc admin` under a limit within 1.5x of its measured {MC_ADMIN_PEAK_BYTES} B peak: {starved}"
