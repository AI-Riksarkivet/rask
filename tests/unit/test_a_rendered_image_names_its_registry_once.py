"""CONTRACT: a rendered first-party image names its registry EXACTLY ONCE, including under the
flags `scripts/ingest-lane.sh` renders the chart with.

`image.repository` and `image.catalog.repository` are two different KINDS of value wearing one key
name, which is this estate's recurring "two answers collapsed into one":

  * ``image.repository`` is the REGISTRY (``localhost:5000``) — ``rask.image`` prefixes it onto every
    component.
  * ``image.catalog.repository`` is a per-component NAME override (default ``lance-rest-catalog``),
    because the catalog family's image is not named after its component. ``lance.catalogImage`` feeds
    it to ``rask.image`` AS THE NAME, which prefixes the registry itself.

Passing a registry-qualified value to the second one therefore renders
``localhost:5000/localhost:5000/lance-rest-catalog:dev``. Measured 2026-09-10: the lane script did
exactly that, and applying the render put ELEVEN catalog-family Deployments plus every backend onto an
image no registry holds — the fleet survived only because Kubernetes keeps the old pods on a surge
rollout, and `rask-maintenance` (which does not surge) went down outright.

The gate renders with the lane's OWN flags, read out of the script, so the two cannot drift: a future
edit that re-qualifies the name override fails here rather than on a live estate.
"""

from __future__ import annotations

import re

from tests.unit.test_invariants import REPO, _rendered_docs


LANE = REPO / "scripts/ingest-lane.sh"
#: The values the script's own defaults give the two shell variables its render interpolates.
_SHELL_DEFAULTS = {"REGISTRY": "localhost:5000", "TAG": "dev"}


def _lane_set_flags() -> tuple[str, ...]:
    """The `--set-string k=v` pairs `render()` passes, with the script's default shell values applied.

    Read from the script rather than restated, so this gate tests what the lane actually does. A
    render() that stops passing image flags at all yields an empty tuple and the assertion below then
    covers the chart's own defaults, which is still the invariant.
    """
    body = LANE.read_text()
    render = re.search(r"^render\(\) \{(.*?)^\}", body, re.DOTALL | re.MULTILINE)
    assert render is not None, "scripts/ingest-lane.sh no longer defines render() — this gate is blind"
    flags = re.findall(r'--set-string\s+([A-Za-z0-9_.]+)="([^"]*)"', render.group(1))
    resolved = []
    for key, raw in flags:
        value = re.sub(r"\$\{?([A-Z_]+)\}?", lambda m: _SHELL_DEFAULTS.get(m.group(1), m.group(0)), raw)
        resolved.append(f"{key}={value}")
    return tuple(resolved)


def _container_images(docs: list[dict]) -> list[tuple[str, str]]:
    """Every (workload name, image) in the render, across the three kinds that carry a pod template."""
    out: list[tuple[str, str]] = []
    for doc in docs:
        if doc.get("kind") not in {"Deployment", "StatefulSet", "Job", "CronJob"}:
            continue
        name = (doc.get("metadata") or {}).get("name", "<unnamed>")
        spec = doc.get("spec") or {}
        template = (spec.get("jobTemplate", {}).get("spec", {}).get("template") if doc["kind"] == "CronJob" else spec.get("template")) or {}
        pod = (template.get("spec")) or {}
        for container in (*pod.get("initContainers", []), *pod.get("containers", [])):
            image = container.get("image")
            if isinstance(image, str) and image:
                out.append((name, image))
    return out


def test_the_lane_render_names_the_registry_once_on_every_image() -> None:
    flags = _lane_set_flags()
    assert flags, "the lane render passes no image flags — the doubling this gate exists for cannot be reproduced"
    docs = _rendered_docs("image.localImages=false", *flags)
    registry = _SHELL_DEFAULTS["REGISTRY"]
    doubled = [(name, image) for name, image in _container_images(docs) if image.count(f"{registry}/") > 1]
    assert not doubled, f"a rendered image names its registry twice — image.catalog.repository is a NAME override, not a registry: {doubled}"


def test_the_catalog_family_renders_under_the_registry_the_lane_pushes_to() -> None:
    """The positive half: the doubling is not fixed by dropping the registry altogether.

    Without this, `--set-string image.catalog.repository=lance-rest-catalog` and deleting the flag
    entirely are indistinguishable from a bare name that resolves to Docker Hub, which is the failure
    `rask.image` was written to refuse.
    """
    docs = _rendered_docs("image.localImages=false", *_lane_set_flags())
    registry = _SHELL_DEFAULTS["REGISTRY"]
    catalog = [image for name, image in _container_images(docs) if name.endswith("-catalog")]
    assert catalog, "no catalog workload rendered — this gate is blind"
    for image in catalog:
        assert image.startswith(f"{registry}/lance-rest-catalog:"), image
