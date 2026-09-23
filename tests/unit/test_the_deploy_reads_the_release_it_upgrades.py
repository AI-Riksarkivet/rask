"""An UPGRADE derives its image settings from what the live release IS, on either kind of estate.

Two estates run this Makefile. A SIDE-LOADED one (`make k3s-build` + `k3s-import`, bare `<stem>:<tag>`
names the node already holds) and a REGISTRY one (`image.repository=localhost:5000`, per-component pins).
`make k3s-up` and `make k3s-converge` must tell them apart by the one value that differs —
`image.repository` — and nothing else: a substring match on `repository:` also matched the retired
`ray.image.repository` that `values-local.yaml` wrote into every release, so a side-loaded release read
as a registry one and was upgraded onto zone tags no host had built.

The retired Ray keys themselves are migrated, not dropped: on the registry estate they were the only
record of which head image was deployed, and ignoring them would re-point the RayCluster at a tag no
build pushed. Every case runs the real recipe against a fake helm (`tests/unit/k3s_recipe.py`).
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

from tests.unit import chart_render
from tests.unit.k3s_recipe import (
    K3S_UP_PREREQUISITES,
    PROBE_TAG,
    REPO,
    UpgradeRefused,
    buildable_stems,
    captured_upgrade,
    rendered_images,
    split_reference,
)


#: A side-loaded release as the previous `make k3s-up` stored it: side-loaded images, and the Ray head
#: override `values-local.yaml` carried then.
SIDE_LOADED = {
    "image": {"localImages": True, "tag": "dev"},
    "ray": {"image": {"repository": "localhost:5000/ray-cluster", "tag": "main-2cfe68f4"}},
}

#: The registry estate: images from its dev registry, per-component pins, and the same Ray override.
REGISTRY = {
    "image": {"localImages": False, "repository": "localhost:5000", "tag": "dev", "tags": {"gateway": "stagerunner-04d6fcda"}},
    "ray": {"image": {"repository": "localhost:5000/ray-cluster", "tag": "main-2cfe68f4"}},
}


def _images(flags: list[str]) -> dict[str, str]:
    """`{owner: image}` for every first-party image the upgrade would render."""
    stems = buildable_stems()
    return {owner: image for owner, image in rendered_images(chart_render.render(*flags)) if split_reference(image)[1] in stems}


def _up(tmp: pathlib.Path, live: dict) -> list[str]:
    return captured_upgrade(tmp, "k3s-up", live=live, up_to_date=K3S_UP_PREREQUISITES, variables={"K3S_TAG": PROBE_TAG})


def test_a_side_loaded_release_is_upgraded_at_the_local_tag_whatever_else_its_values_carry(tmp_path: pathlib.Path) -> None:
    images = _images(_up(tmp_path, SIDE_LOADED))

    wrong = {owner: image for owner, image in images.items() if split_reference(image)[0] or split_reference(image)[2] != PROBE_TAG}

    assert not wrong, f"a side-loaded release must upgrade every first-party image to the bare `<stem>:{PROBE_TAG}` k3s-import loaded: {wrong}"


def test_the_registry_estate_keeps_the_ray_head_it_already_deploys(tmp_path: pathlib.Path) -> None:
    images = _images(_up(tmp_path, REGISTRY))

    assert images.get("RayCluster/rask-ray") == "localhost:5000/ray-cluster:main-2cfe68f4", (
        f"the registry estate's Ray head moved to {images.get('RayCluster/rask-ray')!r}; its live values name "
        "localhost:5000/ray-cluster:main-2cfe68f4, and nothing pushed any other tag of that image"
    )


def test_the_registry_estate_keeps_its_registry_and_pins(tmp_path: pathlib.Path) -> None:
    images = _images(_up(tmp_path, REGISTRY))

    assert images.get("Deployment/rask-gateway") == "localhost:5000/gateway:stagerunner-04d6fcda", images.get("Deployment/rask-gateway")


def test_a_retired_ray_key_naming_ANOTHER_registry_is_refused_not_guessed(tmp_path: pathlib.Path) -> None:
    live = {**REGISTRY, "ray": {"image": {"repository": "registry.example:5000/ray-cluster", "tag": "t1"}}}

    with pytest.raises(UpgradeRefused) as refused:
        _up(tmp_path, live)

    assert "ray.image.repository" in refused.value.stderr, refused.value.stderr


def test_the_chart_refuses_the_retired_ray_keys() -> None:
    """Any other path to helm (a hand-run upgrade, a reconciler) must not silently ignore them."""
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm not available")
    argv = [helm, "template", "rask", str(REPO / "chart"), *chart_render.OIDC_ARGS, *chart_render.DEFAULT_ARGS]

    done = subprocess.run([*argv, "--set", "ray.image.repository=localhost:5000/ray-cluster"], capture_output=True, text=True)  # noqa: S603

    assert done.returncode != 0, "the chart rendered with the retired ray.image.repository and silently ignored it"
    assert "ray.image.name" in done.stderr and "image.tags" in done.stderr, f"the refusal must say what replaces the key: {done.stderr!r}"


def test_converge_rolls_a_stem_on_a_SIDE_LOADED_release(tmp_path: pathlib.Path) -> None:
    flags = captured_upgrade(tmp_path, "k3s-converge", live=SIDE_LOADED, variables={"TAG": "t-new", "K3S_TAG": PROBE_TAG})

    images = _images(flags)

    assert images.get("Deployment/rask-catalog") == "lance-rest-catalog:t-new", images.get("Deployment/rask-catalog")
    assert images.get("Deployment/rask-gateway") == f"gateway:{PROBE_TAG}", images.get("Deployment/rask-gateway")


def test_converge_rolls_a_stem_on_a_REGISTRY_release(tmp_path: pathlib.Path) -> None:
    flags = captured_upgrade(tmp_path, "k3s-converge", live=REGISTRY, variables={"TAG": "t-new"})

    images = _images(flags)

    assert images.get("Deployment/rask-catalog") == "localhost:5000/lance-rest-catalog:t-new", images.get("Deployment/rask-catalog")
    assert images.get("Deployment/rask-gateway") == "localhost:5000/gateway:stagerunner-04d6fcda", images.get("Deployment/rask-gateway")


def test_converge_refuses_a_cluster_with_no_release(tmp_path: pathlib.Path) -> None:
    """Converging is repair of a live estate; on a cluster with no release there is nothing to converge."""
    with pytest.raises(UpgradeRefused) as refused:
        captured_upgrade(tmp_path, "k3s-converge", live=None, variables={"TAG": "t-new"})

    assert "no live release" in refused.value.stderr, refused.value.stderr
