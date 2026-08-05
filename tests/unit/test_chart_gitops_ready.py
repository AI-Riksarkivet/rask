"""The chart is consumed by a GitOps reconciler, and these are the two properties that requires.

Neither was true before 2026-08-04, and both failed the same way: silently at render time, loudly
much later in a cluster nobody was watching.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _helm() -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    return helm


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    argv = [_helm(), "template", "rask", str(CHART)]
    for value in sets:
        argv += ["--set", value]
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


def test_a_bare_image_name_cannot_be_rendered_by_accident() -> None:
    """`image.repository: ""` used to render `gateway:dev` — i.e. `docker.io/library/gateway:dev`.

    It only ever appeared to work because `make k3s-import` had side-loaded the tag into the node's
    containerd, so the kubelet never pulled. A reconciler has no side channel: every pod
    ImagePullBackOffs and the error blames Docker Hub rather than the missing setting. Observed
    2026-08-04 on controlplane:dev and compute:dev after a `helm upgrade` reset the deployments to
    the chart defaults.
    """
    proc = _render()
    assert proc.returncode != 0, "a registry-less render must FAIL, not emit a Docker Hub reference"
    assert "image.repository must be set" in proc.stderr


def test_side_loaded_images_remain_supported_but_only_as_an_explicit_opt_in() -> None:
    """`make k3s-up` and the chart tests both take this path deliberately."""
    proc = _render("image.localImages=true")
    assert proc.returncode == 0, proc.stderr
    assert 'image: "gateway:dev"' in proc.stdout


def test_a_digest_pins_every_first_party_image_and_beats_the_tag() -> None:
    """A tag is a mutable pointer; a digest is what an image-automation controller writes back into
    git, and the only reference that cannot drift under the commit that claims it."""
    proc = _render(
        "image.repository=ghcr.io/example/rask",
        "image.digest=sha256:abc123",
        "frontend.enabled=true",
    )
    assert proc.returncode == 0, proc.stderr
    assert "ghcr.io/example/rask/gateway@sha256:abc123" in proc.stdout
    assert "ghcr.io/example/rask/web-home@sha256:abc123" in proc.stdout
    # …and the tag it would otherwise have used is gone from every first-party ref.
    assert "ghcr.io/example/rask/gateway:dev" not in proc.stdout


# --------------------------------------------------------------------------------------------------
# A zone must never be deployed without the upstreams its routes need
# --------------------------------------------------------------------------------------------------


def _zone_env(rendered: str, zone: str) -> set[str]:
    """Every env NAME on one web zone's Deployment."""
    import re

    for doc in re.split(r"(?m)^---\s*$", rendered):
        if "kind: Deployment" not in doc:
            continue
        if not re.search(rf"(?m)^\s{{0,2}}name:\s*rask-web-{zone}\s*$", doc):
            continue
        return set(re.findall(r"-\s*name:\s*([A-Z_]+_API)\b", doc))
    return set()


def test_the_CATALOG_FAMILY_honours_the_estate_image_contract_too() -> None:
    """Eleven containers run the catalog image with different entrypoints (catalog, lineage, the
    medallion movers, maintenance, explorer, the bootstrap job), and `lance.catalogImage` used to
    render `repository:tag` on its OWN — honouring neither the registry prefix, nor the digest, nor
    localImages. So a chart configured with a registry still emitted the bare `lance-rest-catalog:dev`
    (i.e. Docker Hub) for all eleven, which is why every deploy needed `kubectl set image` fix-ups and
    why no GitOps reconciler could own this release. The helper now delegates to `rask.image`; this
    pins that it stays delegated."""
    proc = _render(
        "image.repository=ghcr.io/example/rask",
        "image.digest=sha256:abc123",
        "explorer.enabled=true",
        "medallion.enabled=true",
    )
    assert proc.returncode == 0, proc.stderr
    assert "ghcr.io/example/rask/lance-rest-catalog@sha256:abc123" in proc.stdout
    # The bare form is the defect itself — it must not survive anywhere in a configured render.
    assert 'image: "lance-rest-catalog:' not in proc.stdout, (
        "a catalog-family container rendered a REGISTRY-LESS image — lance.catalogImage stopped delegating to rask.image"
    )


def test_the_media_zones_deploy_with_NO_upstreams_when_the_plane_is_off() -> None:
    """The render that produced a live 502, pinned so the deploy path has something to be right about.

    `frontend.apps` always includes `explorer` and `annotator`, and both proxy /<zone>/api/* to
    VIEWER_API — env the chart injects only when `explorer.enabled`, which DEFAULTS TO FALSE. So the
    zones render happily with no upstream at all, their BFF falls back to a localhost dev default
    that does not exist in-cluster, and /annotator/api/datasets answers 502. Observed on release
    revision 25, where `explorer` had simply never been set.

    The chart is NOT made to refuse this: a fleet that does not want the corpus volume mounted is
    legitimate, and forcing the plane on every install would be a worse answer. The guarantee lives
    in the deploy path instead — `make k3s-up` defaults EXPLORER=true — and this test states plainly
    what the other setting costs.
    """
    proc = _render("image.localImages=true", "explorer.enabled=false")
    assert proc.returncode == 0, proc.stderr

    for zone in ("annotator", "explorer"):
        assert "VIEWER_API" not in _zone_env(proc.stdout, zone), f"{zone} gained VIEWER_API with the plane off — then this test is stale, not the chart"


def test_make_k3s_up_enables_the_media_plane_by_DEFAULT() -> None:
    """The bug was never in the chart; it was that the documented deploy path never passed the flag.

    Asserted against the Makefile rather than a comment, because the 502 came back the moment the
    default was absent — and a default nobody sets is indistinguishable from one that is wrong.
    """
    makefile = (REPO / "Makefile").read_text()

    assert "EXPLORER ?= true" in makefile, "make k3s-up must default the media plane ON"
    assert "--set explorer.enabled=$(EXPLORER)" in makefile, "k3s-up must PASS the flag — defining it and not using it is the same 502"


def test_enabling_explorer_gives_BOTH_media_zones_every_upstream_they_route_to() -> None:
    """The deploy path `make k3s-up` must take.

    `zone-contract`'s bff-routes suite asserts the other half — that each zone's declared upstreams
    match the routes it actually makes. This asserts the chart can satisfy it: both zones read the
    viewer, both search (the annotator gained `/api/search/similar` with "more like this"), and both
    reach the annotator service.
    """
    proc = _render("image.localImages=true", "explorer.enabled=true")
    assert proc.returncode == 0, proc.stderr

    for zone in ("annotator", "explorer"):
        env = _zone_env(proc.stdout, zone)
        for upstream in ("VIEWER_API", "ANNOTATOR_API", "SEARCH_API"):
            assert upstream in env, f"{zone} renders without {upstream} — its BFF proxy would 502"


def test_the_object_store_does_not_inherit_the_generic_APP_memory_ceiling() -> None:
    """RustFS sits under the whole lakehouse and named no `resources` tier, so it inherited
    `default` — 512Mi, sized for a stateless FastAPI shell. Measured 2026-08-05 on the dev estate:
    370Mi at IDLE and **33 OOMKills**, each one taking every Lance read and write in the cluster
    down with it. The failure is invisible in review (the values file simply lacks a key) and reads
    in the cluster as "the lakehouse is flaky".
    """
    rendered = _render("image.localImages=true", "rustfs.enabled=true")
    assert rendered.returncode == 0, rendered.stderr

    tenants = [doc for doc in yaml.safe_load_all(rendered.stdout) if doc and doc.get("kind") == "Tenant"]
    assert tenants, "the RustFS Tenant did not render — this test would pass vacuously"

    def _mib(quantity: str) -> int:
        text = str(quantity)
        return int(text.removesuffix("Gi")) * 1024 if text.endswith("Gi") else int(text.removesuffix("Mi"))

    for tenant in tenants:
        for pool in tenant["spec"]["pools"]:
            limit = pool["resources"]["limits"]["memory"]
            assert _mib(limit) >= 2048, f"the object store is capped at {limit} — it OOMKills under real load"


def test_the_recoverable_drop_plane_is_actually_WIRED_into_the_catalog() -> None:
    """#122. The trash/undrop/purge plane — ~500 lines, 42 tests, an undrop UI — was inert in every
    chart deployment: `LANCE_TRASH_GRACE_DAYS` was rendered NOWHERE, while two chart comments
    discussed it as an operator knob. Every drop destroyed bytes immediately, `undrop` had nothing to
    restore, and `maintenance.trashPurge` shipped as a toggle whose input could never exist.
    """
    rendered = _render("image.localImages=true")
    assert rendered.returncode == 0, rendered.stderr

    for doc in yaml.safe_load_all(rendered.stdout):
        if not doc or doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] != "catalog":
                continue
            env = {e["name"]: e.get("value") for e in container.get("env", [])}
            assert env.get("LANCE_TRASH_GRACE_DAYS") == "7", "the grace period never reaches the catalog"
            return
    raise AssertionError("no catalog container rendered — this test would pass vacuously")


def test_grace_days_ZERO_survives_the_render() -> None:
    """0 is the meaningful destroy-immediately opt-out — Sprig's default() would eat it (the
    LANCE_MAX_CONCURRENT_WRITES lesson, third occurrence)."""
    rendered = _render("image.localImages=true", "catalog.trashGraceDays=0")
    assert rendered.returncode == 0, rendered.stderr
    assert '{ name: LANCE_TRASH_GRACE_DAYS, value: "0" }' in rendered.stdout.replace("'", '"')
