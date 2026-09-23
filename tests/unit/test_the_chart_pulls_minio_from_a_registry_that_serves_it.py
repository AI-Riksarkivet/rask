"""The object store's images come from a registry that still serves them.

Measured 2026-09-23 against the registry API with an anonymous pull token: Docker Hub answers HTTP 401
for `minio/minio:RELEASE.2025-04-22T22-12-26Z` and `minio/mc:RELEASE.2025-08-13T08-35-41Z`, while
quay.io serves the same tags for amd64 and arm64. A host that has never cached them — every fresh one
— cannot start the store, its bucket Job or its user-provisioning Job, and nothing else in the
lakehouse becomes Ready without them. An existing host keeps working from its image cache, which is
why the pin looked healthy.

Reachability itself is not unit-testable (no network in this suite); what is pinned here is that no
MinIO reference falls back to the bare Docker Hub name, which is the exact regression.
"""

from __future__ import annotations

from tests.unit import chart_render
from tests.unit.k3s_recipe import rendered_images, split_reference


def test_no_minio_image_is_pulled_from_docker_hub() -> None:
    docs = chart_render.render(*chart_render.DEFAULT_ARGS)

    minio = [(owner, image) for owner, image in rendered_images(docs) if "minio/" in image]
    from_hub = [(owner, image) for owner, image in minio if split_reference(image)[0] in {"", "docker.io"}]

    assert minio, "the render names no MinIO image — this gate is looking at the wrong render"
    assert not from_hub, f"these MinIO images resolve to Docker Hub, which refuses them (HTTP 401): {from_hub}"
