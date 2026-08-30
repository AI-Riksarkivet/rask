"""The MEDIA head's own tier is governed, exactly like the events head's and every tier below it.

THE ASYMMETRY THIS CLOSES — the second instance of row 7's shape, in the same service. `produce.py`
now registers the `bronze$events` it seeds, and `transform.py` asks the catalog before every silver
and gold write. `media_produce.py` had no catalog import at all (`grep -c catalog` returned 0): it
wrote `bronze-media$objects` through `ingest_to_bronze` and told the catalog nothing. That dataset
held no `table:` object, so `POST /v1/table/bronze-media$objects/policy/set` answered 404 *"table has
no storage location to police"*, no `_protection/` record could be reached, and no FGA grant could
name it — while the silver-media the very next mover derives from it was governed the whole time.

IT REGISTERS RATHER THAN ASKS, for the same reason the events head does: `MEDALLION_MEDIA_BRONZE_URI`
is a DEPLOYMENT CONTRACT. `chart/templates/medallion.yaml` renders it and the media mover's
`fromNamespace`-derived `MEDALLION_FROM_URI` from one `$mediaBronzeNs` expression, and the
`medallion.media` trigger carries no `from_uri`, so a vended location would leave that mover opening a
path nothing writes to — the media lane's first leg dead, with nothing red.

THE ORDERING RULE IS KEPT: registration strictly precedes the first blob, so no window exists in which
bronze media rows sit on storage that the catalog has no record of.

BLOB TYPING IS UNTOUCHED, and that is asserted here rather than assumed: registration is a metadata-only
HTTP call that happens BEFORE `_seed_and_ingest` and hands it nothing, so the native blob-v2 write
(`ingest_to_bronze`, file format 2.2) still receives exactly the arguments it did before.
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
ROOT = "s3://rask-lance"
MEDIA_BRONZE_URI = f"{ROOT}/medallion/bronze-media"

_RESULT = IngestResult(
    version=3,
    row_count=2,
    source_uris=["s3://rask-lance/media-src/batch/img-a.png", "s3://rask-lance/media-src/batch/img-b.png"],
    fields=[{"name": "payload", "type": "blob"}],
)


class _FakeDapr:
    """Records the media-chain trigger — the one publish the head makes directly."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_: Any) -> None:  # noqa: ANN401
        self.published.append((topic_name, json.loads(data)))


def _settings(**overrides: str) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_S3_ENDPOINT": "http://rustfs:9000",
            "MEDALLION_S3_SECRET_ACCESS_KEY": "k",
            "MEDALLION_MEDIA_SOURCE_BUCKET": "rask-lance",
            "MEDALLION_MEDIA_BRONZE_URI": MEDIA_BRONZE_URI,
            "MEDALLION_CATALOG_URL": CATALOG,
            "MEDALLION_CATALOG_ROOT": ROOT,
            **overrides,
        }
    )


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The order of the two steps that decide whether an ingested media blob is reachable."""
    seen: list[str] = []

    def seed_and_ingest(_settings_arg: MedallionSettings) -> IngestResult:
        seen.append("ingest")
        return _RESULT

    monkeypatch.setattr(media_module, "_seed_and_ingest", seed_and_ingest)
    return seen


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every lineage event the media head hands the outbox."""
    events: list[dict[str, Any]] = []

    async def publish(*_a: object, **kwargs: object) -> None:
        events.append(json.loads(cast("str", kwargs["event_json"])))

    monkeypatch.setattr(media_module.outbox, "publish_lineage_with_outbox", publish)
    return events


def _register_route(status: int = 200) -> respx.Route:
    return respx.post(f"{CATALOG}/v1/table/bronze-media$objects/register").mock(return_value=Response(status, json={"location": "medallion/bronze-media"}))


async def _ingest(settings: MedallionSettings | None = None, dapr: _FakeDapr | None = None) -> dict[str, str]:
    return await media_module.ingest_media(cast("Any", dapr or _FakeDapr()), settings or _settings(), token="idem-media-1")


class TestTheMediaHeadRegistersWhatItLands:
    @respx.mock
    @pytest.mark.asyncio
    async def test_the_bronze_media_dataset_is_registered_at_the_uri_it_writes(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        register = _register_route()

        assert (await _ingest())["status"] == "ingested"

        assert register.called, "the bronze media dataset was never registered — it holds no table: object, so no policy, protection or grant can name it"
        body = json.loads(register.calls.last.request.content)
        assert body["id"] == ["bronze-media", "objects"]
        # RELATIVE, because register_table refuses an absolute URI on the dir backend, and resolved
        # under the catalog's own connection root — where MEDALLION_MEDIA_BRONZE_URI already points.
        assert body["location"] == "medallion/bronze-media"

    @respx.mock
    @pytest.mark.asyncio
    async def test_registration_precedes_the_first_blob(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """The movers' ordering rule (`test_no_rows_without_a_catalog_record`), applied to the media head."""
        _register_route().side_effect = lambda request: steps.append("register") or Response(200, json={"location": "medallion/bronze-media"})

        await _ingest()

        assert steps == ["register", "ingest"], f"got {steps} — blobs on storage the catalog has no record of is exactly the ungoverned state"

    @respx.mock
    @pytest.mark.asyncio
    async def test_an_already_registered_bronze_media_is_the_steady_state(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """Every ingest after the first: 409 is convergence, not a failure — as long as the catalog
        governs the location this head actually writes, which the describe below confirms."""
        _register_route(409)
        respx.post(f"{CATALOG}/v1/table/bronze-media$objects/describe").mock(return_value=Response(200, json={"location": MEDIA_BRONZE_URI}))

        assert (await _ingest())["status"] == "ingested"
        assert steps == ["ingest"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_409_pointing_somewhere_else_is_refused(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """ "Already registered" is not "registered WHERE I write". A head that accepted the difference
        would govern one copy of bronze-media and drive the media chain off another."""
        _register_route(409)
        respx.post(f"{CATALOG}/v1/table/bronze-media$objects/describe").mock(return_value=Response(200, json={"location": "s3://elsewhere/bronze-media"}))

        assert (await _ingest())["status"] == "register_failed"
        assert steps == [], "nothing may be written into a tier the catalog governs elsewhere"


class TestTheMediaChainSurvivesIt:
    @respx.mock
    @pytest.mark.asyncio
    async def test_the_lineage_emit_and_the_media_trigger_are_unchanged(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """Registration must not touch the head's contract with the media mover: one lineage event, then
        the `medallion.media` trigger naming the same namespace/dataset — or the chain stops silently."""
        _register_route()
        dapr = _FakeDapr()

        await _ingest(dapr=dapr)

        assert len(published) == 1, f"the media head must emit exactly one bronze-write event, got {len(published)}"
        output = published[0]["outputs"][0]
        assert (output["namespace"], output["name"]) == ("bronze-media", "bronze-media$objects")
        assert [topic for topic, _ in dapr.published] == ["medallion.media"]
        assert dapr.published[0][1]["dataset"] == "bronze-media$objects"

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_blob_write_is_handed_exactly_what_it_was_before(self, monkeypatch: pytest.MonkeyPatch, published: list[dict[str, Any]]) -> None:
        """Blob typing (v2, file format 2.2) is decided entirely inside `_seed_and_ingest`, and
        `lance_ray`'s write is what strips it — which is why this lane round-trips natively. Registration
        is metadata-only and must not reach that call: it takes the settings object and nothing else,
        exactly as before."""
        _register_route()
        seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def seed_and_ingest(*args: object, **kwargs: object) -> IngestResult:
            seen.append((args, kwargs))
            return _RESULT

        monkeypatch.setattr(media_module, "_seed_and_ingest", seed_and_ingest)

        await _ingest()

        settings = _settings()
        assert len(seen) == 1
        (args, kwargs) = seen[0]
        assert kwargs == {}
        assert len(args) == 1 and isinstance(args[0], type(settings)), "the blob write's argument list changed — the native blob-v2 path is not the one it was"

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_refused_registration_lands_nothing_and_fires_nothing(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """The failure mode is deliberate and matches `/produce`: the request did NOTHING — no bronze
        blobs, no lineage, no media trigger — and the route turns it into 503 + Retry-After, the same
        contract a failed publish already has. Best-effort was the alternative and it reinstates the
        defect silently: an ungoverned tier nobody is told about. This does not strand a chain, because
        the refusal happens before the chain starts."""
        _register_route(503)
        dapr = _FakeDapr()

        assert (await _ingest(dapr=dapr))["status"] == "register_failed"
        assert steps == [], "a media ingest the catalog cannot govern must not report success"
        assert published == [], "no head event, so no half-run media chain on an ungoverned tier"
        assert dapr.published == [], "no trigger, so the media mover never derives from bytes nothing governs"


class TestTheUngovernedShapeIsUnchanged:
    @respx.mock
    @pytest.mark.asyncio
    async def test_no_catalog_url_still_ingests_and_emits(self, steps: list[str], published: list[dict[str, Any]], respx_allows_unused_routes: None) -> None:
        """The dev/demo stack runs with no catalog at all (`test_media_ingest.py`'s settings carry none,
        and the movers keep the same escape hatch). The media head must not acquire a hard dependency."""
        register = _register_route()

        assert (await _ingest(_settings(MEDALLION_CATALOG_URL="")))["status"] == "ingested"

        assert not register.called
        assert steps == ["ingest"] and len(published) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_disabled_head_registers_nothing(self, steps: list[str], published: list[dict[str, Any]], respx_allows_unused_routes: None) -> None:
        """409 media_disabled writes no dataset, so there is nothing to govern — attaching a `table:`
        object to bytes that never arrive is not governance."""
        register = _register_route()

        assert (await _ingest(_settings(MEDALLION_MEDIA_BRONZE_URI="")))["status"] == "media_disabled"

        assert not register.called
        assert steps == []


class TestTheRegistrationIsAddressable:
    @respx.mock
    @pytest.mark.asyncio
    async def test_a_media_bronze_uri_outside_the_catalog_root_fails_closed(
        self, steps: list[str], published: list[dict[str, Any]], respx_allows_unused_routes: None
    ) -> None:
        """`medallion.buckets` can zone a stage namespace into its own bucket, and `register_table` can
        only name a path INSIDE the catalog's connection root — so such a media bronze is unregisterable
        through this door. Fail at the seam naming both URIs; a governed-looking head that silently
        writes ungoverned bytes is the defect this closes. The shipped chart never renders that shape,
        which `test_the_media_head_can_register_the_bronze_it_lands` pins from the other side."""
        _register_route()

        assert (await _ingest(_settings(MEDALLION_MEDIA_BRONZE_URI="s3://another-bucket/medallion/bronze-media")))["status"] == "register_failed"
        assert steps == []


class TestTheRouteTellsTheCallerToRetry:
    """The service status only matters if the door turns it into the retry contract the caller reads.

    Called directly rather than through a TestClient: the claim is the RESPONSE MAPPING (status,
    `Retry-After`, the six-key problem envelope), not request binding, and a route function is where
    that mapping lives.
    """

    @pytest.mark.asyncio
    async def test_a_register_failure_is_a_503_with_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi.responses import JSONResponse

        from medallion.api import ingest_media as route_module

        async def refused(*_a: object, **_kw: object) -> dict[str, str]:
            return {"status": "register_failed", "token": "idem-media-1"}

        monkeypatch.setattr(route_module, "run_ingest_media", refused)

        response = await route_module.ingest_media(cast("Any", _FakeDapr()), _settings(), originator=None, idempotency_key="idem-media-1")

        assert isinstance(response, JSONResponse)
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
        body = json.loads(bytes(response.body))
        # The six-key envelope every medallion problem body carries (`test_problem_bodies_carry_a_code`).
        assert {"type", "title", "status", "detail", "code", "error"} <= set(body)
        assert "catalog registration" in body["detail"], (
            "the detail must name WHICH failure — 'retry' is the same advice for a publish failure, but the thing to look at is not"
        )
