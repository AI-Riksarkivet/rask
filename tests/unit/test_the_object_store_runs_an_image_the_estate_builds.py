"""The object store and its scoped-users hook run an image the estate builds, pinned by content.

`minio/minio` and `minio/mc` refuse anonymous pulls on Docker Hub and quay.io (401 on the manifest,
measured 2026-09-24 and 2026-10-05), so a chart naming either starts the store and completes an
upgrade only on a node that already holds the bytes ([[XC-075]]). The claim pinned here is the render
half: both pods resolve to the estate's configured registry, and the per-component `minio` digest
reaches both, so the server and the hook run one estate-built image and never different builds of it.
"""

from __future__ import annotations

import re

from tests.unit.chart_render import containers, render


DIGEST = "sha256:" + "ab" * 32

#: A registry off this node is what `prod-credentials.yaml` reads as a real deployment, so the dev
#: credentials it refuses are replaced.
_REGISTRY_ARGS = (
    "--set", "image.repository=reg.example",
    "--set", "openbao.devMode=false",
    "--set", "age.password=a-real-secret-value-32-chars-long",
    "--set", "minio.secretKey=a-real-secret-value-32-chars-long",
    "--set", "dapr.appToken=a-real-secret-value-32-chars-long",
    "--set", "signing.provisioned=true",
    "--set", "nats.auth.provisioned=true",
)  # fmt: skip


def _store_images(*extra: str) -> dict[str, str]:
    """`workload/container` -> image for the store's StatefulSet and the scoped-users Job's main containers."""
    found: dict[str, str] = {}
    for workload, name, container in containers(render(*_REGISTRY_ARGS, *extra)):
        workload = re.sub(r"-r\d+$", "", workload)
        if (workload, name) in {("StatefulSet/rask-minio", "minio"), ("Job/rask-minio-scoped-users", "mc")}:
            found[f"{workload}/{name}"] = container["image"]
    return found


def test_the_store_and_its_hook_resolve_to_one_estate_built_image_pinned_by_digest() -> None:
    tagged = _store_images()
    assert set(tagged) == {"StatefulSet/rask-minio/minio", "Job/rask-minio-scoped-users/mc"}, tagged
    for where, image in tagged.items():
        assert image.startswith("reg.example/"), f"{where} names {image!r}, an image outside the estate's registry"

    pinned = _store_images("--set", f"image.digests.minio={DIGEST}")
    assert set(pinned.values()) == {f"reg.example/minio@{DIGEST}"}, pinned
