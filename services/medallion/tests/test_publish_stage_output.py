"""The mover asks the CATALOG to gate and publish what it wrote, instead of gating locally.

Two gates ran the same assertions in two places with different consequences. The catalog's withholds
the `published` TAG — data stays committed but unpublished. The mover's withheld only the next
TRIGGER, so a refused batch was already committed into silver or gold and visible to anyone reading
`latest`; `assert_quality_on_batch` documents that hole in its own docstring. Only the tag is a real
boundary, which is why the design deletes the local gate once the mover publishes.

This is the mover's half of that: one call, the catalog's answer, and a refusal that is a normal
outcome rather than an error — the run did its job, it is the DATA that was refused.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from medallion.services.catalog_register import PublishOutcome, publish_stage_output


CATALOG = "http://catalog.test"


def _route(**body: object) -> respx.Route:
    payload = {"table": "s3://b/t", "published": True, "from_version": 1, "to_version": 2, "assertions": [], "accepted": []}
    return respx.post(f"{CATALOG}/v1/table/silver$features/publish").mock(return_value=httpx.Response(200, json={**payload, **body}))


def _publish(
    *,
    catalog_url: str = CATALOG,
    version: int = 2,
    key_column: str = "id",
    required_columns: tuple[str, ...] = (),
    accept_assertions: tuple[str, ...] = (),
    app_token: str | None = None,
    service_identity: str | None = None,
) -> PublishOutcome:
    return publish_stage_output(
        catalog_url=catalog_url,
        table_id="silver$features",
        version=version,
        key_column=key_column,
        required_columns=required_columns,
        accept_assertions=accept_assertions,
        app_token=app_token,
        service_identity=service_identity,
    )


class TestTheAsk:
    @respx.mock
    def test_it_names_the_version_it_just_wrote(self) -> None:
        """Not "whatever is latest": between this write and this call another writer may have
        committed, and publishing a version nobody gated is the failure the gate exists to prevent."""
        route = _route()

        _publish(version=7)

        assert json.loads(route.calls.last.request.content)["version"] == 7

    @respx.mock
    def test_the_declared_columns_travel(self) -> None:
        """Without them the door runs two assertions where the mover ran five — the breaking-change
        detector, which is the reason the local gate cannot simply be deleted."""
        route = _route()

        _publish(required_columns=("id", "embedding"))

        assert json.loads(route.calls.last.request.content)["required_columns"] == ["id", "embedding"]

    @respx.mock
    def test_it_authenticates_as_a_service(self) -> None:
        route = _route()

        _publish(app_token="stamped", service_identity="service-bronze-to-silver")

        assert route.calls.last.request.headers["x-lance-service-identity"] == "service-bronze-to-silver"


class TestTheAnswer:
    @respx.mock
    def test_a_pass_reports_published_with_the_range(self) -> None:
        _route(published=True, from_version=1, to_version=2)

        outcome = _publish()

        assert outcome.published is True
        assert (outcome.from_version, outcome.to_version) == (1, 2)
        assert outcome.failed_assertions == []

    @respx.mock
    def test_a_REFUSAL_is_a_normal_outcome_naming_what_failed(self) -> None:
        """Not an exception. The run committed its output and did its job; the DATA was refused, and
        the mover needs the assertion names to decide whether a person should be asked."""
        _route(
            published=False,
            assertions=[
                {"assertion": "row_count_positive", "success": False},
                {"assertion": "not_null", "success": True, "column": "id"},
            ],
        )

        outcome = _publish()

        assert outcome.published is False
        assert outcome.failed_assertions == ["row_count_positive"]

    @respx.mock
    def test_an_accepted_finding_is_reported_back(self) -> None:
        """So a resumed promotion can be told apart from a clean one in the run's own lineage."""
        _route(published=True, accepted=["row_count_positive"])

        assert _publish(accept_assertions=("row_count_positive",)).accepted == ["row_count_positive"]


class TestFailurePosture:
    @respx.mock
    def test_an_UNREACHABLE_catalog_raises_so_the_stage_retries(self) -> None:
        """Distinct from a refusal: nobody gated anything, so acking would strand a written output
        that never becomes ready."""
        from medallion.services.catalog_register import RegisterError

        respx.post(f"{CATALOG}/v1/table/silver$features/publish").mock(side_effect=httpx.ConnectError("down"))

        with pytest.raises(RegisterError, match="unreachable"):
            _publish()

    @respx.mock
    def test_a_403_raises_rather_than_reading_as_a_quality_refusal(self) -> None:
        """An authorization failure is not a data verdict. Reading it as one would report a governance
        outage as a bad batch and ask a person to review data that is fine."""
        from medallion.services.catalog_register import RegisterError

        respx.post(f"{CATALOG}/v1/table/silver$features/publish").mock(return_value=httpx.Response(403, json={"detail": "nope"}))

        with pytest.raises(RegisterError):
            _publish()

    def test_no_catalog_url_refuses_at_the_seam(self) -> None:
        from medallion.services.catalog_register import RegisterError

        with pytest.raises(RegisterError, match="MEDALLION_CATALOG_URL"):
            _publish(catalog_url="")
