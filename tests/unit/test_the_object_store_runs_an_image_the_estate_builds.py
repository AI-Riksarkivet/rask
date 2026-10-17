"""The object store's scoped-users hook runs an image the estate builds, pinned by content.

`minio/mc` refuses anonymous pulls on Docker Hub and quay.io (401 on the manifest, measured 2026-09-24
and 2026-10-05), so a chart naming it completes an upgrade only on a node that already holds the bytes
([[XC-075]]). The claim pinned here is the render half: the hook resolves to the estate's configured
registry, and the per-component `minio` digest reaches it. The development store's server is the minio
fixture's ([[XC-131]]), pinned in its own values to the same build.
"""

from __future__ import annotations

import re

from tests.unit.chart_render import SECRET_STORE_URL, containers, render


DIGEST = "sha256:" + "ab" * 32

#: A registry off this node is what `prod-credentials.yaml` reads as a real deployment, so the dev
#: credentials it refuses are replaced.
_REGISTRY_ARGS = (
    "--set", "image.repository=reg.example",
    "--set", "openbao.enabled=false",
    "--set-string", f"openbao.externalAddr={SECRET_STORE_URL}",
    "--set", "dex.enabled=false",
    "--set", "auth.oidc.connectors.ldap=corp",
    "--set", "signing.provisioned=true",
    "--set", "nats.auth.provisioned=true",
)  # fmt: skip


def _store_images(*extra: str) -> dict[str, str]:
    """`workload/container` -> image for every container of the scoped-users Job, its derive step included."""
    found: dict[str, str] = {}
    for workload, name, container in containers(render(*_REGISTRY_ARGS, *extra)):
        workload = re.sub(r"-r\d+$", "", workload)
        if workload == "Job/rask-minio-scoped-users":
            found[f"{workload}/{name}"] = container["image"]
    return found


def test_the_scoped_users_hook_resolves_to_an_estate_built_image_pinned_by_digest() -> None:
    tagged = _store_images()
    assert set(tagged) == {"Job/rask-minio-scoped-users/derive", "Job/rask-minio-scoped-users/mc"}, tagged
    for where, image in tagged.items():
        assert image.startswith("reg.example/"), f"{where} names {image!r}, an image outside the estate's registry"

    pinned = _store_images("--set", f"image.digests.minio={DIGEST}")
    assert set(pinned.values()) == {f"reg.example/minio@{DIGEST}"}, pinned
