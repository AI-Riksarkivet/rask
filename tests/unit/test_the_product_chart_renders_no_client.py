"""The product chart renders no client of the lakehouse: deleting every client changes nothing in it (XC-175).

rask's annotator (XC-167, ADR 0171), its media plane, viewer and search (XC-180), and flows (XC-179) are clients of the
lakehouse, as Label Studio or a user's own service would be, so nothing the product chart renders names one: no workload,
ServiceAccount, route, state-store scope, bus user, store user, OpenFGA subject, secret-store role or Dapr component. The
exception is the web zones, frontends this chart still serves: every object named for a zone, and the path the Gateway
routes to it.

Read from the render in the three overlays that ship: the default values, the deployed estate (`values-local.yaml`)
and production. Every string a rendered object holds, its keys included, is searched, so a client's name in a
ConfigMap's script or a policy document fails as surely as one in a Deployment.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, PROD_ARGS, REPO, render


_OVERLAYS: dict[str, tuple[str, ...]] = {
    "default": DEFAULT_ARGS,
    "deployed": ("--set", "image.localImages=true", "-f", str(REPO / "deploy/values-local.yaml")),
    "prod": PROD_ARGS,
}

#: client -> what names it in a rendered string. `search` alone is Postgres's `search_path`, so the media plane is matched
#: by its own objects and identities.
_CLIENTS: dict[str, re.Pattern[str]] = {
    "annotator": re.compile(r"annotator|-assist"),
    "media": re.compile(r"viewer|rask-search|sa-search|media-s3|media-corpus|/api/explorer|corpus", re.IGNORECASE),
    "flows": re.compile(r"\bflows\b"),
}

#: A zone is the component `web-<zone>`, its objects `<release>-web-<zone>`, and the Gateway routes it at `/<zone>`.
_ZONE_OBJECT = re.compile(r"^(rask-)?web-[a-z]+$")
_ZONE_PATH = re.compile(r"^/[a-z]+$")


def _the_zones(doc: dict[str, Any], value: str) -> bool:
    """Whether ``value`` is a zone's: inside an object named for a zone, naming one, or the path the Gateway routes one at."""
    return bool(_ZONE_OBJECT.match(str(doc.get("metadata", {}).get("name", "")))) or bool(_ZONE_OBJECT.match(value) or _ZONE_PATH.match(value))


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


@pytest.mark.parametrize("client", list(_CLIENTS))
@pytest.mark.parametrize("overlay", list(_OVERLAYS))
def test_nothing_the_product_chart_renders_names_a_client_but_a_zone(overlay: str, client: str) -> None:
    docs = render(*_OVERLAYS[overlay])
    named = sorted(
        f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')} {path}: {value[:120]!r}"
        for doc in docs
        for path, value in _strings(doc, "")
        if _CLIENTS[client].search(value) and not _the_zones(doc, value)
    )
    assert not named, f"the product chart renders {client}'s object, scope, identity or secret:\n" + "\n".join(named)
