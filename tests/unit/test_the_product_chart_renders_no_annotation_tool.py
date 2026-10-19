"""The product chart renders no annotation tool: the lakehouse knows none (XC-167, ADR 0171).

rask's annotator is a client of the lakehouse with its own release (deploy/fixtures/annotator), as Label Studio would be,
so nothing the product chart renders names it: no workload, ServiceAccount, route, state-store scope, bus user, OpenFGA
subject, secret-store role or Dapr component. The one exception is the annotator ZONE, a frontend this chart still
serves: every object named for it, and every mention of it elsewhere (its route, its front-door admission).

Read from the render in the three overlays that ship: the default values, the deployed estate (`values-local.yaml`)
and production. Every string a rendered object holds, its keys included, is searched, so an annotator name in a
ConfigMap's script or a policy document fails as surely as one in a Deployment.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, PROD_ARGS, REPO, render


_OVERLAYS: dict[str, tuple[str, ...]] = {
    "default": (*DEFAULT_ARGS, "--set", "explorer.enabled=true"),
    "deployed": ("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml")),
    "prod": PROD_ARGS,
}

#: The zone's own names: its Deployment, Service and image, and the path the Gateway routes to it.
_ZONE = ("web-annotator", "/annotator")


def _the_zones(doc: dict[str, Any], value: str) -> bool:
    """Whether ``value`` is the zone's: inside an object named for the zone, or naming the zone itself."""
    return str(doc.get("metadata", {}).get("name", "")).endswith(_ZONE[0]) or any(name in value for name in _ZONE)


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every string in a rendered object, keys included, with the path it sits at."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield f"{path}.{key}", str(key)
            yield from _strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _strings(child, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


@pytest.mark.parametrize("overlay", list(_OVERLAYS))
def test_nothing_the_product_chart_renders_names_the_annotator_but_its_zone(overlay: str) -> None:
    docs = render(*_OVERLAYS[overlay])
    named = sorted(
        f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')} {path}: {value[:120]!r}"
        for doc in docs
        for path, value in _strings(doc, "")
        if ("annotator" in value.lower() or "-assist" in value) and not _the_zones(doc, value)
    )
    assert not named, "the product chart renders an annotation tool's object, scope, identity or secret:\n" + "\n".join(named)
