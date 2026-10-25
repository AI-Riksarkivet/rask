"""The product chart renders no client of the lakehouse: deleting every client changes nothing in it (XC-175).

rask's annotator (XC-167, ADR 0171), its media plane, viewer and search (XC-180), and flows (XC-179) are clients of the
lakehouse, as Label Studio or a user's own service would be, and so are the web zones (XC-181), so nothing the product
chart renders names one: no workload, ServiceAccount, route, state-store scope, bus user, store user, OpenFGA subject,
secret-store role, Dapr component, session or sign-in secret, or network policy admitting one by label.

Read from the render in the three overlays that ship: the default values, the deployed estate (`values-local.yaml`)
and production. Every string a rendered object holds, its keys included, is searched, so a client's name in a
ConfigMap's script or a policy document fails as surely as one in a Deployment.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

import pytest
import yaml

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
    "zones": re.compile(r"\bweb-[a-z]+\b|rask-web\b|sa-web\b|frontend-session|oidc-client|RASK_EDGE_URL|SESSION_SECRET"),
}


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
def test_nothing_the_product_chart_renders_names_a_client(overlay: str, client: str) -> None:
    docs = render(*_OVERLAYS[overlay])
    named = sorted(
        f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')} {path}: {value[:120]!r}"
        for doc in docs
        for path, value in _strings(doc, "")
        if _CLIENTS[client].search(value)
    )
    assert not named, f"the product chart renders {client}'s object, scope, identity or secret:\n" + "\n".join(named)


#: The values a deployment sets to admit a client of its own release by its ServiceAccount (owner, 2026-10-25, XC-183),
#: and the lakehouse objects each one feeds: bootstrap-admin writes a decider's tuple, and the catalog reads the
#: provenance maintainers from its environment. Admitting a client may change these and nothing else.
_EXTENSION_POINTS: dict[str, set[tuple[str, str]]] = {
    "auth.externalDeciders": {("Job", "rask-bootstrap-admin")},
    "services.lineage.provenanceMaintainers": {("Deployment", "rask-catalog")},
}

_DEPLOYED: dict[str, Any] = yaml.safe_load((REPO / "deploy/values-local.yaml").read_text())


def _deployed_value(path: str) -> Any:
    value: Any = _DEPLOYED
    for key in path.split("."):
        value = value[key]
    return value


def _without_clients() -> tuple[str, ...]:
    return tuple(arg for path in _EXTENSION_POINTS for arg in ("--set-json", f"{path}=[]"))


@pytest.mark.parametrize("point", list(_EXTENSION_POINTS))
def test_admitting_a_client_changes_only_the_objects_its_extension_point_feeds(point: str) -> None:
    """The removal test (XC-175): a client release coming or going changes no lakehouse object but the one it is granted by.

    Rendered as the deployed estate runs, once with every client unnamed and once naming the clients `values-local.yaml`
    admits at this point. The Ray engine's address (`ray.*`) is the engine's, not a client's, and is left as deployed.
    """
    absent = render(*_OVERLAYS["deployed"], *_without_clients())
    present = render(*_OVERLAYS["deployed"], *_without_clients(), "--set-json", f"{point}={json.dumps(_deployed_value(point))}")

    def by_name(docs: Iterable[dict[str, Any]]) -> dict[tuple[str, str], str]:
        return {(doc.get("kind", ""), doc.get("metadata", {}).get("name", "")): json.dumps(doc, sort_keys=True) for doc in docs}

    before, after = by_name(absent), by_name(present)
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    assert _deployed_value(point), f"values-local.yaml admits no client at {point}, so this case compares two equal renders"
    assert changed == _EXTENSION_POINTS[point]
