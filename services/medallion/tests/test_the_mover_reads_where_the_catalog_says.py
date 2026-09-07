"""A mover opens the upstream the CATALOG vends, not the path its chart composed.

The other half of rule I2, and the half without which the media head's fix strands the lane. The head
now asks the catalog where `bronze-media$objects` lives and writes there
(`test_the_media_head_asks_where_its_bronze_lives`), and names that location on the
`medallion.media` trigger. `_confine_from_uri` honours a trigger-supplied upstream only INSIDE
`read_root`, and for a project-less trigger `read_root` was `MEDALLION_FROM_URI` — a composed
`{root}/medallion/{namespace}` in the reserved platform bucket. The vended location lives in the
warehouse the namespace is BOUND to, i.e. a different bucket, so the mover would have refused the
head's own answer as unconfined and DROPped every media arrival.

`_confine_from_uri` names this limit outright — "Making it WORK single-tenant means resolving a real
storage root here — the catalog's connection root is the candidate, and it is not free". This takes
the tighter option than that candidate: not a connection root, not a bucket, but the location the
catalog states for THIS stage's own upstream table. That NARROWS what a trigger may name (a specific
dataset URI rather than everything under a root), so it cannot widen what this mover's credentials
will open.

Three answers, three behaviours, and the difference between them is the point:
  * the catalog states a location  -> that IS the upstream, and the confinement root
  * the catalog governs no such table (4xx) -> compose, which is correct for an external producer
  * the catalog cannot be ASKED (5xx/unreachable) -> RAISE, so the handler RETRIES rather than
    silently reading a path that may not be the governed one
"""

from __future__ import annotations

from typing import Any, cast

import pytest
import respx
from httpx import Response

from medallion.core.config import MedallionSettings
from medallion.services import catalog_register
from medallion.services.transform import _confine_from_uri, _resolve_roots
from medallion.services.trigger_guards import StageTrigger


CATALOG = "http://catalog.test"
ROOT = "s3://lance-catalog"
#: What `chart/templates/medallion.yaml` renders for the media mover.
COMPOSED_FROM = f"{ROOT}/medallion/bronze-media"
#: What the catalog vends, because `bronze-media` is bound to a warehouse. Different BUCKET.
VENDED_FROM = "s3://lakehouse-wh/medallion/bronze-media"
FROM_DATASET = "bronze-media$objects"


def _settings(**overrides: str) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "MEDALLION_S3_ENDPOINT": "http://rustfs:9000",
            "MEDALLION_S3_SECRET_ACCESS_KEY": "k",
            "MEDALLION_FROM_NAMESPACE": "bronze-media",
            "MEDALLION_FROM_DATASET": FROM_DATASET,
            "MEDALLION_TO_NAMESPACE": "silver-media",
            "MEDALLION_TO_DATASET": "silver-media$features",
            "MEDALLION_FROM_URI": COMPOSED_FROM,
            "MEDALLION_TO_URI": f"{ROOT}/medallion/silver-media",
            "MEDALLION_CATALOG_URL": CATALOG,
            "MEDALLION_CATALOG_ROOT": ROOT,
            **overrides,
        }
    )


def _describe(location: str, status: int = 200) -> respx.Route:
    return respx.post(f"{CATALOG}/v1/table/{FROM_DATASET}/describe").mock(
        return_value=Response(status, json={"table_uri": location, "location": location} if status == 200 else {"detail": "no"})
    )


def _confined(trigger_uri: str, roots: Any) -> str | None:  # noqa: ANN401 — StageRoots, private to transform
    return _confine_from_uri(
        StageTrigger(token="t", dataset=FROM_DATASET, namespace="bronze-media", from_uri=trigger_uri),
        from_uri=roots.from_uri,
        read_root=roots.read_root,
        transition="bronze-media->silver-media",
        token="t",
        project="",
    )


@respx.mock
@pytest.mark.asyncio
async def test_the_vended_location_becomes_the_upstream_and_the_confinement_root() -> None:
    """The property the media lane needs: the head's own answer is accepted, not refused."""
    _describe(VENDED_FROM)
    roots = await _resolve_roots(_settings(), project="", from_dataset=FROM_DATASET)

    assert roots.from_uri == VENDED_FROM, f"the mover would open {roots.from_uri}, which no catalog vends"
    assert roots.read_root == VENDED_FROM, "the confinement root still names the composed path"
    assert _confined(VENDED_FROM, roots) == VENDED_FROM, "the head's own trigger was refused as unconfined"


@respx.mock
@pytest.mark.asyncio
async def test_a_table_the_catalog_does_not_govern_keeps_the_composed_path() -> None:
    """`None` is an ANSWER, not a failure — an external OpenLineage producer writing an unregistered
    dataset is real and supported, and composing is the right behaviour for it."""
    _describe("", status=403)
    roots = await _resolve_roots(_settings(), project="", from_dataset=FROM_DATASET)

    assert roots.from_uri == COMPOSED_FROM
    assert roots.read_root == COMPOSED_FROM


@respx.mock
@pytest.mark.asyncio
async def test_a_catalog_that_cannot_be_ASKED_raises_rather_than_composing() -> None:
    """A 5xx is not "no such table". Collapsing the two would let an outage read as a governance
    answer and send this mover at a path that may not be the governed copy — the exact class of
    silent divergence that made `/ingest-media` 503 for days."""
    _describe("", status=503)

    with pytest.raises(catalog_register.RegisterError):
        await _resolve_roots(_settings(), project="", from_dataset=FROM_DATASET)


@respx.mock
@pytest.mark.asyncio
async def test_no_catalog_url_is_the_ungoverned_dev_shape() -> None:
    """The movers' standing escape hatch: nothing to ask, so the deployment contract is the answer."""
    roots = await _resolve_roots(_settings(MEDALLION_CATALOG_URL=""), project="", from_dataset=FROM_DATASET)

    assert roots.from_uri == COMPOSED_FROM
    assert roots.read_root == COMPOSED_FROM


@respx.mock
@pytest.mark.asyncio
async def test_a_trigger_naming_somewhere_ELSE_is_still_refused() -> None:
    """Narrowing, not widening. The confinement is what stops a topic anything in the mesh can publish
    to from becoming a read primitive for every bucket this mover's credentials can reach."""
    _describe(VENDED_FROM)
    roots = await _resolve_roots(_settings(), project="", from_dataset=FROM_DATASET)

    assert _confined("s3://somewhere-else/data", roots) is None, "an unconfined upstream was honoured"
    assert _confined(cast("str", f"{VENDED_FROM}/sub/path"), roots) == f"{VENDED_FROM}/sub/path", "a path BENEATH the vended location must still be allowed"
