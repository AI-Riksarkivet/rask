"""The span that NAMES the cascade head must cover the half that fails, and say so when it does.

open_fastapi-audit — "The cascade head's span closes before the half that fails, and never sets ERROR
status — unlike every other manual span in the estate".

THE FINDING NARROWS ITSELF and this file keeps the narrowed claim. The request is NOT green
end-to-end: `api/produce.py` turns `publish_failed` into a 503 problem+json, and the producer runs
under `opentelemetry-instrument`, so the FastAPI server span records the 5xx and goes ERROR. Nobody is
blind at the HTTP layer.

What is wrong is the span an operator would actually filter on. `medallion.produce` covered the Lance
SEED only — it closed before `build_run_event` and before the outbox publish — so a run whose cascade
never fired left a `medallion.produce` span reporting success. Two consequences, and the second is the
worse one:

* **It reports success for a failed run.** A trace filtered to the operation's own name misattributes
  the failure to the transport rather than to the operation.
* **It vanished entirely when `compute_enabled` is false.** No seed, no `with` block, no span — so the
  case where the head emits a SYNTHETIC event (`result is None`) had no span at all, and that is
  precisely the configuration where the cascade is most worth watching.

`observability.md` warns against wrapping a whole ROUTE body, because that duplicates the auto-created
HTTP span. This is not a route: it is the service function the route calls, and the rule it is held to
is the other one — the span must cover the operation it names, not a prefix of it.

The estate already had the convention on four other spans (`ingest/workflow.py`,
`medallion/workflow.py`, `flows/activities.py`, `maintenance/sweep.py` all call `set_status`); it was
broken on the two that matter most.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from medallion.core.config import MedallionSettings
from medallion.services import produce as produce_module


def _recording_tracer():
    """A tracer whose spans land in memory, and the exporter that holds them."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer(__name__), exporter


def _seeder(**_: object):
    class _Result:
        version = 1
        row_count = 8
        size_bytes = 4096
        fields = None

    def seed(uri: str, storage_options: dict[str, str], **kwargs: object) -> object:
        return _Result()

    return seed


def _settings(**overrides: str) -> MedallionSettings:
    return MedallionSettings.model_validate({"MEDALLION_COMPUTE_ENABLED": "true", "MEDALLION_BRONZE_URI": "memory://bronze", **overrides})


async def _failing_publish(*args: object, **kwargs: object) -> None:
    raise RuntimeError("the bus is unreachable")


async def _ok_publish(*args: object, **kwargs: object) -> None:
    return None


def _span(exporter, name: str):
    return next((s for s in exporter.get_finished_spans() if s.name == name), None)


@pytest.mark.asyncio
async def test_the_head_span_goes_ERROR_when_the_cascade_never_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding: a failed publish left `medallion.produce` reporting success."""
    from opentelemetry.trace import StatusCode

    tracer, exporter = _recording_tracer()
    monkeypatch.setattr(produce_module, "tracer", tracer)
    monkeypatch.setattr(produce_module, "seed_bronze", _seeder())
    monkeypatch.setattr(produce_module.outbox, "publish_lineage_with_outbox", _failing_publish)

    result = await produce_module.produce(cast("Any", None), _settings(), token="idem-error")
    assert result["status"] == "publish_failed", f"the failure path did not run: {result}"

    span = _span(exporter, "medallion.produce")
    assert span is not None, "no `medallion.produce` span was recorded at all"
    assert span.status.status_code is StatusCode.ERROR, (
        "the span that NAMES the cascade head reports success for a run whose cascade never fired — "
        "so a trace filtered to `medallion.produce` misattributes the failure"
    )


@pytest.mark.asyncio
async def test_the_head_span_COVERS_the_publish_not_just_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covering only a prefix of the operation is what let the status be wrong in the first place.

    Proved by ordering rather than by reading the source: if the span closed at the seed, the publish
    would run after it ended, and a span cannot be ended twice — so recording the publish's own child
    span INSIDE the head span is only possible if the head is still open.
    """
    tracer, exporter = _recording_tracer()
    monkeypatch.setattr(produce_module, "tracer", tracer)
    monkeypatch.setattr(produce_module, "seed_bronze", _seeder())

    async def publish_with_a_child(*args: object, **kwargs: object) -> None:
        with tracer.start_as_current_span("test.publish"):
            pass

    monkeypatch.setattr(produce_module.outbox, "publish_lineage_with_outbox", publish_with_a_child)
    await produce_module.produce(cast("Any", None), _settings(), token="idem-cover")

    head, child = _span(exporter, "medallion.produce"), _span(exporter, "test.publish")
    assert head is not None and child is not None, "one of the two spans was never recorded"
    assert child.parent is not None and child.parent.span_id == head.context.span_id, (
        "the publish ran outside `medallion.produce`, so the span covers only the seed — a prefix of the operation it names"
    )


@pytest.mark.asyncio
async def test_the_head_span_EXISTS_when_compute_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no seed there was no `with` block and therefore no span — in exactly the configuration
    where the head emits a synthetic event and is most worth watching."""
    tracer, exporter = _recording_tracer()
    monkeypatch.setattr(produce_module, "tracer", tracer)
    monkeypatch.setattr(produce_module.outbox, "publish_lineage_with_outbox", _ok_publish)

    await produce_module.produce(cast("Any", None), _settings(MEDALLION_COMPUTE_ENABLED="false"), token="idem-nocompute")

    assert _span(exporter, "medallion.produce") is not None, "no span at all when compute is disabled, so the synthetic-head path is untraceable"


@pytest.mark.asyncio
async def test_a_successful_head_is_not_marked_ERROR(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure mode that would hide the fix: marking everything ERROR proves nothing."""
    from opentelemetry.trace import StatusCode

    tracer, exporter = _recording_tracer()
    monkeypatch.setattr(produce_module, "tracer", tracer)
    monkeypatch.setattr(produce_module, "seed_bronze", _seeder())
    monkeypatch.setattr(produce_module.outbox, "publish_lineage_with_outbox", _ok_publish)

    result = await produce_module.produce(cast("Any", None), _settings(), token="idem-ok")
    assert result["status"] == "produced", f"the happy path did not run: {result}"

    span = _span(exporter, "medallion.produce")
    assert span is not None and span.status.status_code is not StatusCode.ERROR
    assert span.attributes.get("lance.write.row_count") == 8, "the seed attributes were lost in the move"


# ── the media head, the same two defects ────────────────────────────────────────────────────────
#
# `medallion.ingest_media` has the identical shape: the span wraps the seed and closes before BOTH
# publish attempts (the lineage emit and the media-chain trigger), each of which has its own `except`
# returning `publish_failed` and neither of which touches the span.


def _media_settings() -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_BRONZE_URI": "memory://bronze",
            # The four `media_head_enabled` actually gates on — read off the predicate, not guessed.
            # A skipped test proves nothing, and a head that never enables would make this file
            # silently assert about the media path without ever running it.
            "MEDALLION_S3_ENDPOINT": "http://rustfs.invalid:9000",
            # `MedallionSettings` refuses an S3 endpoint with no credential ("every Lance write would
            # 403") — a fail-closed validator, not an obstacle. The endpoint is unroutable and nothing
            # in this test reaches S3; the seeder is replaced.
            "MEDALLION_S3_ACCESS_KEY_ID": "test-key",
            "MEDALLION_S3_SECRET_ACCESS_KEY": "test-secret",
            "MEDALLION_MEDIA_BRONZE_URI": "memory://media-bronze",
            "MEDALLION_MEDIA_SOURCE_BUCKET": "source",
        }
    )


@pytest.mark.asyncio
async def test_the_media_head_span_goes_ERROR_when_its_emit_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same defect, same span-names-the-operation argument, on the multimodal head."""
    from medallion.services import media_produce as media_module
    from opentelemetry.trace import StatusCode

    assert media_module.media_head_enabled(_media_settings()), (
        "the fixture does not enable the media head, so this test would assert nothing — "
        "`media_head_enabled` requires compute + s3_endpoint + media_bronze_uri + media_source_bucket"
    )

    tracer, exporter = _recording_tracer()
    monkeypatch.setattr(media_module, "tracer", tracer)
    monkeypatch.setattr(media_module, "_seed_and_ingest", lambda settings: _SeedResult())
    monkeypatch.setattr(media_module.outbox, "publish_lineage_with_outbox", _failing_publish)

    result = await media_module.ingest_media(cast("Any", None), _media_settings(), token="idem-media")
    assert result["status"] == "publish_failed", f"the failure path did not run: {result}"

    span = _span(exporter, "medallion.ingest_media")
    assert span is not None, "no `medallion.ingest_media` span was recorded"
    assert span.status.status_code is StatusCode.ERROR, "the media head reports success for a run whose chain never fired"


class _SeedResult:
    version = 1
    row_count = 3
    size_bytes = 128
    fields = None
    source_uris: tuple[str, ...] = ("iiif://vol/00012.jpg",)
