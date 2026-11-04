"""The product chart renders no client of the lakehouse: deleting every client changes nothing in it (XC-175).

rask's annotator (XC-167, ADR 0171), its media plane, viewer and search (XC-180), and flows (XC-179) are clients of the
lakehouse, as Label Studio or a user's own service would be, and so are the web zones (XC-181), the bring-your-own
engine and its companions (ADR 0189): the Ray engine, compute, Kueue, the compaction job and the model-download token
a runner uses. So nothing the product chart renders names one: no workload, ServiceAccount, route, state-store scope,
bus user, store user, OpenFGA subject, secret-store role, Dapr component, session or sign-in secret, scrape job, alert
or network policy admitting one by label.

Read from the render in the three overlays that ship: the default values, the deployed estate (`values-local.yaml`)
and production, each with no client admitted at an extension point; a client's own release supplies its admission
(XC-191), and the second test holds an admission to the objects it feeds.
Every string a rendered object holds, its keys included, is searched, so a client's name in a ConfigMap's
script or a policy document fails as surely as one in a Deployment. What the gate cannot read is stated: an object a
template renders only behind a switch none of these overlays turns on, and NOTES.txt, which an install prints and
`helm template` does not render.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from typing import Any

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, PROD_ARGS, REPO, render


#: A deployed release's values can hold a key a client once owned: revision 35's (`helm get values rask`, 2026-11-02)
#: held the inbox's email channel under `services.notifications`. The deployed overlay carries it, so such a key is shown
#: to render nothing.
_STALE_CLIENT_VALUES = '{"channels":{"email":{"enabled":true,"host":"rask-mailpit","port":1025,"secretName":"notifications-smtp"}}}'

_OVERLAYS: dict[str, tuple[str, ...]] = {
    "default": DEFAULT_ARGS,
    "deployed": (
        "--set",
        "image.localImages=true",
        "-f",
        str(REPO / "deploy/values-local.yaml"),
        "--set-json",
        f"services.notifications={_STALE_CLIENT_VALUES}",
    ),
    "prod": PROD_ARGS,
}

#: client -> what names it in a rendered string. `search` alone is Postgres's `search_path` and `compute` alone a verb, so
#: the media plane and compute are matched by their own objects, identities and routes; `ray` is matched as a word, so
#: an `array` is not the engine.
_CLIENTS: dict[str, re.Pattern[str]] = {
    "annotator": re.compile(r"annotator|-assist"),
    "media": re.compile(r"viewer|rask-search|sa-search|media-s3|media-corpus|/api/explorer|corpus", re.IGNORECASE),
    "flows": re.compile(r"\bflows\b"),
    "zones": re.compile(r"\bweb-[a-z]+\b|rask-web\b|sa-web\b|frontend-session|oidc-client|RASK_EDGE_URL|SESSION_SECRET"),
    "compute": re.compile(r"^compute$|[-_]compute\b|\bcompute[-_]|\bcompute (?:fixture|service|release)|/api/(?:ray|serve)\b", re.IGNORECASE),
    "ray": re.compile(r"(?<![a-z])ray(?![a-z])|kuberay|ray(?:auth|dashboard|cluster|service|job)", re.IGNORECASE),
    "kueue": re.compile(r"kueue", re.IGNORECASE),
    "table-maintenance": re.compile(r"table-maintenance", re.IGNORECASE),
    "hf": re.compile(r"hf[-_]?token|hugging\s*face", re.IGNORECASE),
    # NATS's reply subjects are `_INBOX.>`, which every bus user subscribes to; the inbox client is any other `inbox`.
    "notifications": re.compile(r"notification|(?<![_a-z])inbox", re.IGNORECASE),
}

#: The values a deployment sets to admit a client of its own release by its ServiceAccount (owner, 2026-10-25, XC-183),
#: each with the value that admits nobody and the lakehouse objects it feeds: bootstrap-admin writes a client's estate
#: roles, and the catalog reads the provenance maintainers from its environment. They are the only values that may name
#: a client, and admitting one may change these objects and nothing else.
_EXTENSION_POINTS: dict[str, tuple[str, set[tuple[str, str]]]] = {
    "auth.externalDeciders": ("{}", {("Job", "rask-bootstrap-admin")}),
    "services.lineage.provenanceMaintainers": ("[]", {("Deployment", "rask-catalog")}),
}


def _without_clients() -> tuple[str, ...]:
    return tuple(arg for path, (nobody, _) in _EXTENSION_POINTS.items() for arg in ("--set-json", f"{path}={nobody}"))


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
    docs = render(*_OVERLAYS[overlay], *_without_clients())
    named = sorted(
        f"{doc.get('kind')}/{doc.get('metadata', {}).get('name')} {path}: {value[:120]!r}"
        for doc in docs
        for path, value in _strings(doc, "")
        if _CLIENTS[client].search(value)
    )
    assert not named, f"the product chart renders {client}'s object, scope, identity or secret:\n" + "\n".join(named)


#: What a client's own release supplies at each extension point to be admitted (XC-191: the lakehouse's overlays name no
#: client, so the admission is the client's, and a generic one stands in for it here).
_ADMITTED: dict[str, Any] = {
    "auth.externalDeciders": {"default:a-client": ["decider"]},
    "services.lineage.provenanceMaintainers": ["default:a-client"],
}


@pytest.mark.parametrize("point", list(_EXTENSION_POINTS))
def test_admitting_a_client_changes_only_the_objects_its_extension_point_feeds(point: str) -> None:
    """The removal test (XC-175): a client release coming or going changes no lakehouse object but the one it is granted by.

    Rendered as the deployed estate runs, once with every client unnamed and once admitting the one a client's release
    supplies at this point.
    """
    absent = render(*_OVERLAYS["deployed"], *_without_clients())
    present = render(*_OVERLAYS["deployed"], *_without_clients(), "--set-json", f"{point}={json.dumps(_ADMITTED[point])}")

    def by_name(docs: Iterable[dict[str, Any]]) -> dict[tuple[str, str], str]:
        return {(doc.get("kind", ""), doc.get("metadata", {}).get("name", "")): json.dumps(doc, sort_keys=True) for doc in docs}

    before, after = by_name(absent), by_name(present)
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    assert changed == _EXTENSION_POINTS[point][1]
