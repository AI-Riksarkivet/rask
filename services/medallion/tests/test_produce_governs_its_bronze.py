"""The cascade HEAD's own tier is governed, exactly like every tier below it.

THE ASYMMETRY THIS CLOSES. `transform.py` asks the catalog before every silver and gold write, and the
separate ingest plane registers the bronze it lands — but `POST /produce` seeded `bronze$events` with
no catalog call at all. The dataset held no `table:` object, so `POST /v1/table/bronze$events/policy/set`
answered 404 *"table has no storage location to police"*, no `_protection/` record could be reached, and
no FGA grant could name it. The same tier was governed or not purely by which door produced it.

THE PRODUCER REGISTERS RATHER THAN ASKS, and that is the ONE place it departs from the movers. A mover
takes the location the catalog vends (`ensure_stage_output`) because nothing else names where its output
lives. The producer's write location is a DEPLOYMENT CONTRACT: `chart/templates/medallion.yaml` renders
`MEDALLION_BRONZE_URI` and the bronze->silver mover's `MEDALLION_FROM_URI` from the same expression, so
the location is stated by the chart rather than asked for. The head keeps its URI and ATTACHES it through
`register_table`, the door built for bytes written outside the catalog's own doors: it needs no warehouse,
which is why it works in the reserved platform bucket the medallion lives in.

Which writer created the table no longer decides whether the cascade reads anything: `/bronze-arrival`
resolves the arrived table's location through the catalog and names it on the trigger
(`tests/unit/test_bronze_arrival_carries_the_vended_location.py`).

The ORDERING rule is kept: registration strictly precedes the first row, so there is no window in which
bronze rows exist that the catalog has no record of.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
import respx
from httpx import Response

from medallion.core.config import MedallionSettings
from medallion.services import ingest_trigger
from medallion.services import produce as produce_module


CATALOG = "http://catalog.test"
ROOT = "s3://rask-lance"
BRONZE_URI = f"{ROOT}/medallion/bronze"


class _Result:
    version = 3
    row_count = 8
    size_bytes = 4096
    fields = None


def _settings(**overrides: str) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_BRONZE_URI": BRONZE_URI,
            "MEDALLION_CATALOG_URL": CATALOG,
            "MEDALLION_CATALOG_ROOT": ROOT,
            **overrides,
        }
    )


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The order of the two steps that decide whether an unregistered bronze row is reachable."""
    seen: list[str] = []

    def seed(uri: str, storage_options: dict[str, str], **kwargs: object) -> object:
        seen.append("seed")
        return _Result()

    monkeypatch.setattr(produce_module, "seed_bronze", seed)
    return seen


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every lineage event the head hands the outbox — the cascade's only trigger."""
    events: list[dict[str, Any]] = []

    async def publish(*_a: object, **kwargs: object) -> None:
        events.append(json.loads(cast("str", kwargs["event_json"])))

    monkeypatch.setattr(produce_module.outbox, "publish_lineage_with_outbox", publish)
    return events


def _register_route(status: int = 200) -> respx.Route:
    return respx.post(f"{CATALOG}/v1/table/bronze$events/register").mock(return_value=Response(status, json={"location": "medallion/bronze"}))


async def _produce(settings: MedallionSettings | None = None) -> dict[str, str]:
    return await produce_module.produce(cast("Any", None), settings or _settings(), token="idem-1")


class TestTheHeadRegistersWhatItSeeds:
    @respx.mock
    @pytest.mark.asyncio
    async def test_the_bronze_dataset_is_registered_at_the_uri_it_writes(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        register = _register_route()

        assert (await _produce())["status"] == "produced"

        assert register.called, "the bronze dataset was never registered — it holds no table: object, so no policy, protection or grant can name it"
        body = json.loads(register.calls.last.request.content)
        assert body["id"] == ["bronze", "events"]
        # RELATIVE, because register_table refuses an absolute URI on the dir backend, and resolved
        # under the catalog's own connection root — which is where MEDALLION_BRONZE_URI already points.
        assert body["location"] == "medallion/bronze"

    @respx.mock
    @pytest.mark.asyncio
    async def test_registration_precedes_the_first_row(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """The movers' ordering rule (`test_no_rows_without_a_catalog_record`), applied to the head."""
        _register_route().side_effect = lambda request: steps.append("register") or Response(200, json={"location": "medallion/bronze"})

        await _produce()

        assert steps == ["register", "seed"], f"got {steps} — rows on disk the catalog has no record of is exactly the ungoverned state"

    @respx.mock
    @pytest.mark.asyncio
    async def test_an_already_registered_bronze_is_the_steady_state(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """Every produce after the first: 409 is convergence, not a failure — as long as the catalog
        governs the location this head actually writes, which the describe below confirms."""
        _register_route(409)
        respx.post(f"{CATALOG}/v1/table/bronze$events/describe").mock(return_value=Response(200, json={"location": BRONZE_URI}))

        assert (await _produce())["status"] == "produced"
        assert steps == ["seed"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_409_pointing_somewhere_else_is_refused(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """ "Already registered" is not "registered WHERE I write". A head that accepted the difference
        would govern one copy of bronze and cascade off another."""
        _register_route(409)
        respx.post(f"{CATALOG}/v1/table/bronze$events/describe").mock(return_value=Response(200, json={"location": "s3://somewhere-else/bronze"}))

        assert (await _produce())["status"] == "register_failed"
        assert steps == [], "nothing may be written into a tier the catalog governs elsewhere"


class TestTheCascadeHeadSurvivesIt:
    @respx.mock
    @pytest.mark.asyncio
    async def test_the_bronze_write_event_is_still_emitted_exactly_once(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        _register_route()

        await _produce()

        assert len(published) == 1, f"the cascade head must emit exactly one bronze-write event, got {len(published)}"

    @respx.mock
    @pytest.mark.asyncio
    async def test_the_event_still_carries_what_bronze_arrival_matches_on(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """Registration must not touch the head's contract with `/bronze-arrival`: the same COMPLETE
        event, the same output namespace/name, the same token — or the whole cascade stops silently."""
        _register_route()

        await _produce()

        assert ingest_trigger._bronze_write_dataset(published[0], _settings(), "") == "bronze$events"

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_refused_registration_writes_nothing_and_fires_nothing(self, steps: list[str], published: list[dict[str, Any]]) -> None:
        """The failure mode is stated deliberately: the request did NOTHING — no bronze rows, no head
        event, no half-run cascade — and the route turns it into 503 + Retry-After, the same contract a
        failed publish already has. Best-effort was the alternative and it reinstates the defect
        silently: an ungoverned tier nobody is told about."""
        _register_route(503)

        assert (await _produce())["status"] == "register_failed"
        assert steps == [], "a bronze seed that the catalog cannot govern must not report success"
        assert published == [], "no head event, so no cascade half-runs on an ungoverned tier"


class TestTheUngovernedShapeIsUnchanged:
    @respx.mock
    @pytest.mark.asyncio
    async def test_no_catalog_url_still_seeds_and_emits(self, steps: list[str], published: list[dict[str, Any]], respx_allows_unused_routes: None) -> None:
        """The dev/demo stack runs with no catalog at all (`test_no_catalog_url_still_writes_to_its_
        configured_uri` pins the mover's half). It must not acquire a hard dependency here."""
        register = _register_route()

        assert (await _produce(_settings(MEDALLION_CATALOG_URL="")))["status"] == "produced"

        assert not register.called
        assert steps == ["seed"] and len(published) == 1


class TestTheRegistrationIsAddressable:
    @respx.mock
    @pytest.mark.asyncio
    async def test_a_bronze_uri_outside_the_catalog_root_fails_closed(
        self, steps: list[str], published: list[dict[str, Any]], respx_allows_unused_routes: None
    ) -> None:
        """`register_table` can only name a path INSIDE the catalog's connection root, so a bronze zoned
        into its own bucket is unregisterable through this door. Fail at the seam naming the two URIs —
        a governed-looking head that silently writes ungoverned bytes is the defect this closes."""
        _register_route()

        assert (await _produce(_settings(MEDALLION_BRONZE_URI="s3://another-bucket/medallion/bronze")))["status"] == "register_failed"
        assert steps == []


class TestAnAttachIsNotAnArrival:
    """The head fires on a batch LANDING, not on a metadata change that names the same table.

    Registering the head's own tier made the catalog publish a `register_table` marker onto the very
    topic `/bronze-arrival` consumes — a COMPLETE event whose one output is `bronze`/`bronze$events`,
    identical on every field the head matched on. Measured: without this guard one `/produce` fired TWO
    cascades, the second over a batch that had not moved, carrying a token nothing else in the run has.
    """

    @pytest.mark.parametrize("operation", ["register_table", "deregister_table", "declare_table"])
    def test_a_byte_free_catalog_marker_fires_nothing(self, operation: str) -> None:
        event = {
            "eventType": "COMPLETE",
            "run": {"runId": "r1", "facets": {"lance": {"operation": operation}}},
            "outputs": [{"namespace": "bronze", "name": "bronze$events"}],
        }

        assert ingest_trigger._bronze_write_dataset(event, _settings(), "") is None

    def test_a_real_bronze_write_still_fires(self) -> None:
        """The denylist must not become an allowlist: an external OpenLineage producer names its own
        operations, or none at all, and it has to keep driving the cascade exactly as before."""
        for lance in ({"operation": "insert"}, {}):
            event = {
                "eventType": "COMPLETE",
                "run": {"runId": "r1", "facets": {"lance": lance}},
                "outputs": [{"namespace": "bronze", "name": "bronze$events"}],
            }

            assert ingest_trigger._bronze_write_dataset(event, _settings(), "") == "bronze$events"


class TestItGovernsWhatThisCallWrites:
    @respx.mock
    @pytest.mark.asyncio
    async def test_a_pure_emit_head_registers_nothing(self, steps: list[str], published: list[dict[str, Any]], respx_allows_unused_routes: None) -> None:
        """With compute off the head writes no dataset — it only announces one. Attaching a `table:`
        object to bytes that never arrive is not governance, and it would make a demo that needs no
        object store fail on a URI it was never going to open."""
        register = _register_route()

        assert (await _produce(_settings(MEDALLION_COMPUTE_ENABLED="false")))["status"] == "produced"

        assert not register.called
        assert steps == [] and len(published) == 1
