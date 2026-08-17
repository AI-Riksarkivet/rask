"""Unit tests for the media ingest head (§9) — contracts of ``ingest_media`` with a fake Dapr client.

Infra-free: the blocking seed+ingest half is monkeypatched (its Lance/S3 halves are covered by the
ingest + transform unit tests and the live e2e); what's pinned here is the HEAD's contract — disabled
config → explicit refusal (no dummy media), emit-then-trigger ordering + topics, publish failure → the
retryable status the route maps to 503.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from dapr.aio.clients import DaprClient
from medallion.core.config import MedallionSettings
from medallion.services.ingest import IngestResult
from medallion.services.media_produce import ingest_media


class _FakeDapr:
    """Records publish_event calls; optionally fails to exercise the publish-failure path."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_: Any) -> None:
        if self.fail:
            raise RuntimeError("sidecar down")
        self.published.append((topic_name, json.loads(data)))


def _settings(**overrides: Any) -> MedallionSettings:
    values: dict[str, Any] = {
        "compute_enabled": True,
        "media_source_bucket": "lake",
        "media_bronze_uri": "s3://lake/medallion/bronze-media",
        "s3_endpoint": "http://rustfs:9000",
        "s3_secret_access_key": "k",
    }
    values.update(overrides)
    return MedallionSettings.model_validate(values)


_RESULT = IngestResult(
    version=3,
    row_count=2,
    source_uris=["s3://lake/media-src/batch/img-a.png", "s3://lake/media-src/batch/img-b.png"],
    fields=[{"name": "payload", "type": "blob"}],
)


def _emit_and_capture(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run the head and return the lineage event it published."""
    import medallion.services.media_produce as mod

    monkeypatch.setattr(mod, "_seed_and_ingest", lambda _s: _RESULT)
    dapr = _FakeDapr()
    asyncio.run(ingest_media(cast(DaprClient, dapr), _settings()))
    return dapr.published[0][1]


def test_disabled_head_refuses_instead_of_dummying() -> None:
    """No media config → an explicit media_disabled (the route's 409): real media can't be faked."""
    dapr = _FakeDapr()
    settings = MedallionSettings.model_validate({})  # compute off, no media settings
    result = asyncio.run(ingest_media(cast(DaprClient, dapr), settings))
    assert result == {"status": "media_disabled"}
    assert dapr.published == []


def test_ingest_emits_lineage_then_publishes_media_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    import medallion.services.media_produce as mod

    monkeypatch.setattr(mod, "_seed_and_ingest", lambda _s: _RESULT)
    dapr = _FakeDapr()
    settings = _settings()

    result = asyncio.run(ingest_media(cast(DaprClient, dapr), settings))

    assert result["status"] == "ingested"
    topics = [topic for topic, _ in dapr.published]
    # Emit BEFORE trigger: the graph must never show a derived silver before its bronze head exists.
    assert topics == [settings.lineage_topic, settings.media_topic]
    event = dapr.published[0][1]
    assert event["outputs"][0]["name"] == "bronze-media$objects"
    # One lineage input per source object — the source-URI → bronze provenance in the data path.
    # Namespace + name REASSEMBLE to the source URI: the split exists so the namespace keeps the URI
    # scheme and `is_external_source` recognises it (R23), not to drop information.
    assert [f"{i['namespace']}/{i['name']}" for i in event["inputs"]] == _RESULT.source_uris
    assert event["outputs"][0]["facets"]["schema"]["fields"] == [{"name": "payload", "type": "blob"}]
    trigger = dapr.published[1][1]
    assert trigger["dataset"] == "bronze-media$objects"
    assert trigger["token"] == result["token"]


def test_publish_failure_surfaces_as_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    import medallion.services.media_produce as mod

    monkeypatch.setattr(mod, "_seed_and_ingest", lambda _s: _RESULT)
    result = asyncio.run(ingest_media(cast(DaprClient, _FakeDapr(fail=True)), _settings()))
    assert result["status"] == "publish_failed"  # → the route's 503 + Retry-After


# --- the media head's source inputs must be recognisable as EXTERNAL ------------------------------
#
# `is_external_source` is literally `"://" in namespace` (lineage/api/fga_deps.py), and R23 rests on it:
# raw is the external world, never a governed tier, so an input naming an external source has no
# `table:` object and no tuple that could ever be written for it.
#
# This head emitted `("source", uri)` — a namespace with no scheme. So every media ingest event named
# an input that read as a GOVERNED dataset called `table:source`, which nobody holds a grant on. Under
# FGA the whole event is then dropped from `GET /events` for EVERY caller, because the feed shows a row
# only if the reader can see every dataset it references. The estate's own convention is a URI-shaped
# namespace: the source-uri split convention is `iiif://vol/00012.jpg` -> (`iiif://vol`, `00012.jpg`).


def test_each_source_input_is_namespaced_so_it_reads_as_external(monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.api.fga_deps import is_external_source

    event = _emit_and_capture(monkeypatch)

    namespaces = [i["namespace"] for i in event["inputs"]]
    assert namespaces, "the head emitted no inputs at all"
    unrecognised = [n for n in namespaces if not is_external_source(n)]
    assert not unrecognised, f"these source namespaces do not read as external, so the event is hidden from every caller under FGA: {unrecognised}"
