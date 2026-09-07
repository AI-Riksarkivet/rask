"""The media head ASKS the catalog where its bronze lives, and writes there.

WHY THE TELLING FORM CANNOT WORK FOR THIS HEAD, measured on the deployed estate 2026-09-06:

    bronze$events         -> s3://lance-catalog/medallion/bronze          (no warehouse binding)
    bronze-media$objects  -> s3://lakehouse-wh/medallion/bronze-media     (bound to lakehouse-wh)
    MEDALLION_MEDIA_BRONZE_URI = s3://lance-catalog/medallion/bronze-media

`register_written_dataset` sends a location RELATIVE to `MEDALLION_CATALOG_ROOT`, and the catalog
resolves it against the namespace's WAREHOUSE BINDING — not against the caller's root. So the two
disagreed, `_require_same_location` correctly refused, and `POST /ingest-media` answered 503
`media ingest catalog registration failed` with a `Retry-After` no retry could satisfy.

Re-registering cannot repair it while the binding stands: `register_table` refuses an absolute URI
(`undrop` pays for this too), so a caller cannot dictate its own location, and `lance-catalog` is in
`reserved_bucket_set` — no warehouse may ever claim it — so a BOUND top-level namespace can never
resolve into the platform root. The events head only works because `bronze` happens to carry no
binding. That is an accident of this estate, not a contract.

So the head asks, exactly as `transform.py` already does for every silver and gold write, and it
NAMES THE ANSWER ON THE TRIGGER (`from_uri`) — which is what `/bronze-arrival` already does for the
tabular lane (`test_bronze_arrival_carries_the_vended_location`). Without that field the media stage runner
falls through to its composed `{root}/medallion/{namespace}` and opens a path nothing writes to: the
lane's first leg dead, with nothing red.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
import respx
from httpx import Response

from medallion.core.config import MedallionSettings
from medallion.services import media_produce as media_module
from medallion.services.ingest import IngestResult


CATALOG = "http://catalog.test"
ROOT = "s3://lance-catalog"
#: What the chart renders — the platform root, which no warehouse may claim.
COMPOSED_URI = f"{ROOT}/medallion/bronze-media"
#: What the catalog actually vends, because `bronze-media` is warehouse-bound. A DIFFERENT BUCKET is
#: the whole point: an assertion that passes when the two coincide proves nothing.
VENDED_URI = "s3://lakehouse-wh/medallion/bronze-media"

_RESULT = IngestResult(
    version=3,
    row_count=2,
    source_uris=["s3://lance-catalog/media-src/batch/img-a.png"],
    fields=[{"name": "payload", "type": "blob"}],
)


class _FakeDapr:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish_event(self, *, topic_name: str, data: str, **_: Any) -> None:  # noqa: ANN401 — `pubsub_name` and the rest ride `**_`
        self.published.append((topic_name, json.loads(data)))


def _settings(**overrides: str) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_S3_ENDPOINT": "http://rustfs:9000",
            "MEDALLION_S3_SECRET_ACCESS_KEY": "k",
            "MEDALLION_MEDIA_SOURCE_BUCKET": "lance-catalog",
            "MEDALLION_MEDIA_BRONZE_URI": COMPOSED_URI,
            "MEDALLION_CATALOG_URL": CATALOG,
            "MEDALLION_CATALOG_ROOT": ROOT,
            **overrides,
        }
    )


@pytest.fixture
def wrote_to(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The URI the blob write was actually handed — the assertion this suite exists for."""
    seen: list[str] = []

    def seed_and_ingest(_settings_arg: MedallionSettings, bronze_uri: str) -> IngestResult:
        seen.append(bronze_uri)
        return _RESULT

    monkeypatch.setattr(media_module, "_seed_and_ingest", seed_and_ingest)
    return seen


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def publish(*_a: object, **kwargs: object) -> None:
        events.append(json.loads(cast("str", kwargs["event_json"])))

    monkeypatch.setattr(media_module.outbox, "publish_lineage_with_outbox", publish)
    return events


def _describe(location: str = VENDED_URI, status: int = 200) -> respx.Route:
    """The catalog's answer to "where does this table live". `table_uri` first, per `_stated_location`."""
    return respx.post(f"{CATALOG}/v1/table/bronze-media$objects/describe").mock(
        return_value=Response(status, json={"table_uri": location, "location": location})
    )


async def _ingest(settings: MedallionSettings | None = None, dapr: _FakeDapr | None = None) -> dict[str, str]:
    return await media_module.ingest_media(cast("Any", dapr or _FakeDapr()), settings or _settings(), token="idem-media-1")


@respx.mock
@pytest.mark.asyncio
async def test_the_blobs_land_where_the_CATALOG_says_not_where_the_chart_composed(wrote_to: list[str], published: list[dict[str, Any]]) -> None:
    """The defect, stated as the property. The composed URI is in the reserved platform bucket; the
    vended one is in the warehouse the namespace is bound to. Writing to the former is what made the
    catalog and the writer govern two different copies of one table."""
    del published  # requested to patch the outbox; the write's URI is the assertion
    _describe()
    result = await _ingest()

    assert result["status"] == "ingested", result
    assert wrote_to == [VENDED_URI], f"the head wrote to {wrote_to}, not the location the catalog vends"


@respx.mock
@pytest.mark.asyncio
async def test_the_media_trigger_NAMES_the_upstream_it_wrote(wrote_to: list[str], published: list[dict[str, Any]]) -> None:
    """Rule I2 from the consuming end. The stage runner composes `{root}/medallion/{namespace}`; without
    `from_uri` it opens that path, finds none of these rows, and acks 200 — the lane dead, nothing red."""
    del wrote_to, published  # requested to patch the write and the outbox; the trigger is the assertion
    _describe()
    dapr = _FakeDapr()
    await _ingest(dapr=dapr)

    triggers = [payload for topic, payload in dapr.published if topic.endswith("media")]
    assert triggers, f"no media trigger published: {dapr.published}"
    assert triggers[0].get("from_uri") == VENDED_URI, triggers[0]


@respx.mock
@pytest.mark.asyncio
async def test_the_lineage_event_names_the_same_location(wrote_to: list[str], published: list[dict[str, Any]]) -> None:
    """A graph that names the composed path describes a dataset nobody wrote."""
    del wrote_to  # requested to patch the write; the emitted event is the assertion
    _describe()
    await _ingest()

    assert published, "no lineage event emitted"
    outputs = published[0].get("outputs") or []
    assert outputs, published[0]
    assert VENDED_URI in json.dumps(outputs), f"the emitted output does not name {VENDED_URI}: {outputs}"


@respx.mock
@pytest.mark.asyncio
async def test_asking_precedes_the_first_blob(wrote_to: list[str], published: list[dict[str, Any]]) -> None:
    """The ordering rule is unchanged by the direction of the question: no window exists in which
    bronze media rows sit on storage the catalog has no record of."""
    del published  # requested to patch the outbox; the order of ask-then-write is the assertion
    route = _describe()
    await _ingest()

    assert route.called, "the head never asked the catalog"
    assert wrote_to, "the head never wrote"


@respx.mock
@pytest.mark.asyncio
async def test_no_catalog_url_keeps_the_configured_uri(wrote_to: list[str], published: list[dict[str, Any]]) -> None:
    """The ungoverned dev/demo shape the stage runners keep the same escape hatch for — there is nothing to
    ask, so the deployment contract is the only answer available."""
    del published  # requested to patch the outbox
    await _ingest(_settings(MEDALLION_CATALOG_URL=""))

    assert wrote_to == [COMPOSED_URI], wrote_to


@respx.mock
@pytest.mark.asyncio
async def test_a_catalog_that_cannot_be_ASKED_lands_nothing_and_fires_nothing(wrote_to: list[str], published: list[dict[str, Any]]) -> None:
    """Fail-closed, and the failure is deliberate: the request did NOTHING — no bronze blobs, no
    lineage, no media trigger — and the route turns it into 503 + Retry-After, the same contract a
    failed publish already has. Best-effort was the alternative and it reinstates the defect silently:
    an ungoverned tier nobody is told about. No chain is stranded, because this precedes the chain."""
    # BOTH doors, because `ensure_stage_output` falls through to CREATE when describe does not answer
    # 200 — a describe that 503s is not yet a refusal, it is a table that may simply not exist.
    _describe(status=503)
    respx.post(f"{CATALOG}/v1/table/bronze-media$objects/create").mock(return_value=Response(503, json={"detail": "catalog down"}))
    dapr = _FakeDapr()

    assert (await _ingest(dapr=dapr))["status"] == "register_failed"
    assert wrote_to == [], "a media ingest the catalog cannot govern must not report success"
    assert published == [], "no head event, so no half-run media chain on an ungoverned tier"
    assert dapr.published == [], "no trigger, so the media stage runner never derives from bytes nothing governs"


@respx.mock
@pytest.mark.asyncio
async def test_the_blob_write_gets_the_settings_and_the_resolved_uri_AND_NOTHING_ELSE(monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]) -> None:
    """Blob typing (v2, file format 2.2) is decided entirely inside `_seed_and_ingest`. Resolving the
    location is a metadata-only HTTP call that must not reach into that write: it hands it the settings
    and the URI the catalog vended, and nothing more."""
    del published  # requested to patch the outbox
    _describe()
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def seed_and_ingest(*args: object, **kwargs: object) -> IngestResult:
        seen.append((args, kwargs))
        return _RESULT

    monkeypatch.setattr(media_module, "_seed_and_ingest", seed_and_ingest)
    await _ingest()

    assert len(seen) == 1
    args, kwargs = seen[0]
    assert kwargs == {}
    assert len(args) == 2 and isinstance(args[0], MedallionSettings) and args[1] == VENDED_URI, (
        f"the blob write's argument list changed — the native blob-v2 path is not the one it was: {args}"
    )
