"""The shared publish helper (§9 P1 claim-check guard + the sidecar timeout, 2026-07-11).

Every publish site in the repo funnels through ``service_kit.dapr_publish.publish_event`` (grep-verified
at review time: catalog + compaction lineage emitters, medallion produce/media/transform/train/
ingest_trigger), so enforcing the claim-check invariant HERE covers them all:
- payload > MAX_PAYLOAD_BYTES (900 KiB, just under NATS's ~1 MiB) → ``ValueError`` BEFORE any I/O —
  the broker would have refused it anyway, this fails fast with the rule's name;
- payload > WARN_PAYLOAD_BYTES (64 KiB) → published, plus a ``dapr_publish_payload_large`` warning
  (facet-bloat early visibility, never a behavior change);
- and the pre-existing contract: a hung sidecar raises ``TimeoutError`` instead of pinning the worker.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from service_kit import dapr_publish


class _Publisher:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, **kwargs: Any) -> None:
        self.published.append(kwargs)


def test_oversize_payload_raises_before_any_io() -> None:
    # ASSERTS: >900KiB → ValueError naming the claim-check rule; the sidecar is NEVER called.
    publisher = _Publisher()
    huge = "x" * (dapr_publish.MAX_PAYLOAD_BYTES + 1)
    with pytest.raises(ValueError, match="POINTERS"):
        asyncio.run(dapr_publish.publish_event(publisher, timeout_seconds=5, topic_name="medallion.bronze", data=huge))
    assert publisher.published == []


def test_large_payload_warns_but_still_publishes(caplog: pytest.LogCaptureFixture) -> None:
    # ASSERTS: 64KiB < payload ≤ 900KiB → published EXACTLY as before, plus the early-warning log.
    publisher = _Publisher()
    big = "x" * (dapr_publish.WARN_PAYLOAD_BYTES + 1)
    with caplog.at_level(logging.WARNING):
        asyncio.run(dapr_publish.publish_event(publisher, timeout_seconds=5, topic_name="lineage.events.v1", data=big))
    assert len(publisher.published) == 1 and publisher.published[0]["data"] == big
    assert any(r.message == "dapr_publish_payload_large" for r in caplog.records)


def test_normal_payload_publishes_silently(caplog: pytest.LogCaptureFixture) -> None:
    # ASSERTS: the default path is byte-identical to before the guard — no warning, one publish.
    publisher = _Publisher()
    with caplog.at_level(logging.WARNING):
        asyncio.run(dapr_publish.publish_event(publisher, timeout_seconds=5, topic_name="training.jobs", data='{"token":"t1"}'))
    assert len(publisher.published) == 1
    assert not [r for r in caplog.records if r.message == "dapr_publish_payload_large"]


def test_bytes_payloads_are_measured_too() -> None:
    # ASSERTS: the guard measures bytes payloads the same as str (both are what our emitters send).
    publisher = _Publisher()
    with pytest.raises(ValueError, match="POINTERS"):
        asyncio.run(
            dapr_publish.publish_event(
                publisher,
                timeout_seconds=5,
                topic_name="t",
                data=b"x" * (dapr_publish.MAX_PAYLOAD_BYTES + 1),
            )
        )
    assert publisher.published == []


def test_hung_sidecar_still_raises_timeout() -> None:
    # ASSERTS: the original contract survives the guard — a wedged sidecar → TimeoutError,
    # which every caller already maps (mover RETRY / best-effort swallow).
    class _Hung:
        async def publish_event(self, **_kw: Any) -> None:
            await asyncio.sleep(60)

    with pytest.raises(TimeoutError):
        asyncio.run(dapr_publish.publish_event(_Hung(), timeout_seconds=0.05, topic_name="t", data="{}"))
