"""Every image the object store's lane runs is pinned by content.

The object-store hook (the scoped users and the platform buckets) runs rask's own lakehouse image, which carries python, boto3 and
botocore, so it resolves to the estate's configured registry and follow the per-component `lance-rest-catalog`
digest an upgrade pins ([[XC-075]]): no third-party CLI image is pulled at upgrade time. The development store itself
runs the upstream `rustfs/rustfs` image from its fixture (`deploy/fixtures/rustfs`), and a tag alone is mutable, so it
names a digest.
"""

from __future__ import annotations

from tests.unit.chart_render import containers, render, render_fixture


DIGEST = "sha256:" + "ab" * 32

#: The component of the object-store hook (`chart/templates/object-store-scoped-users.yaml`).
_HOOKS = frozenset({"object-store-scoped-users"})

#: The dev estate as it pulls: the registry on this node (`localhost:5000`), with the dev object store whose hook this
#: renders. A registry off the node is a real deployment, which refuses the dev store (`prod-credentials.yaml`).
_REGISTRY_ARGS = ("--set", "image.repository=localhost:5000")


def _hook_images(*extra: str) -> dict[str, str]:
    """`component/container` -> image for every container of the object-store hooks."""
    docs = render(*_REGISTRY_ARGS, *extra)
    hooks = tuple(
        doc for doc in docs if doc.get("kind") == "Job" and doc["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component") in _HOOKS
    )
    found: dict[str, str] = {}
    for workload, name, container in containers(hooks):
        found[f"{workload}/{name}"] = container["image"]
    return found


def test_the_object_store_hooks_run_the_estates_lakehouse_image_pinned_by_digest() -> None:
    tagged = _hook_images()
    components = {
        doc["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component") for doc in render(*_REGISTRY_ARGS) if doc.get("kind") == "Job"
    }
    assert components >= _HOOKS, f"the render carries {sorted(components & _HOOKS)} of the object-store hooks {sorted(_HOOKS)}"
    for where, image in tagged.items():
        assert image.startswith("localhost:5000/lance-rest-catalog:"), f"{where} names {image!r}, not the estate's lakehouse image"

    pinned = _hook_images("--set", f"image.digests.lance-rest-catalog={DIGEST}")
    assert set(pinned.values()) == {f"localhost:5000/lance-rest-catalog@{DIGEST}"}, pinned


def test_the_development_store_runs_its_upstream_image_by_digest() -> None:
    images = [
        container["image"]
        for doc in render_fixture("rustfs")
        if doc.get("kind") == "StatefulSet"
        for container in doc["spec"]["template"]["spec"]["containers"]
    ]
    assert images, "the rustfs fixture renders no StatefulSet"
    for image in images:
        repository, _, digest = image.partition("@")
        assert repository.split(":", 1)[0].endswith("rustfs/rustfs"), f"the store runs {image!r}, not the upstream RustFS image"
        assert digest.startswith("sha256:") and len(digest) == len(DIGEST), f"the store's image {image!r} names no content digest"
