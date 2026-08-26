"""`GET /api/media-clip/{doc_id}` ran an unauthenticated libx264 transcode behind a global lock.

open_fastapi-audit HIGH, re-verified at HEAD. Three defects in one route, and they compound:

1. **No auth.** `media.py` contained ZERO auth constructs — the module's whole `Depends` count was
   nought — while its siblings gate on `can_get_metadata` (`datasets.py`) and `can_read_data`
   (`pages.py`). The viewer sits behind the gateway row `("/api/explorer", "/api", *viewer)` and the
   ingress publishes `/api`, so this was reachable from outside.

2. **A process-global lock, waited on unboundedly.** `clips.py` serialises every build on one
   `threading.Lock` with a 120 s ffmpeg timeout, and the route was a plain `def` — so FastAPI
   threadpools it and each waiter holds a POOL THREAD for as long as the queue ahead of it takes.
   The audit measured 41 concurrent requests exhausting the pool. `/livez` stays green throughout,
   because liveness does not know the pool is gone.

3. **A cache key that is a free miss generator.** `clip_cache_path` keys on `int(lo * 1000)`, so
   `?hi=1.001` and `?hi=1.002` are different files. A caller varying the third decimal never hits the
   cache and every request is a fresh transcode — which is what turns (2) from a queue into a weapon.

The three are fixed together on purpose: gating alone leaves an authenticated caller able to do it,
and bounding alone leaves it open to the internet.
"""

from __future__ import annotations

import pytest
from viewer.services import clips


class TestTheCacheKeyDoesNotMissOnNoise:
    """A window an operator cannot perceive the difference of must reuse the same clip."""

    def test_windows_within_the_quantum_share_a_file(self) -> None:
        a = clips.clip_cache_path("ds--doc", 1.0, 9.001)
        b = clips.clip_cache_path("ds--doc", 1.0, 9.002)
        assert a == b, (
            "hi=9.001 and hi=9.002 produce different cache files, so a caller varying the third decimal transcodes on every request and the cache never helps"
        )

    def test_genuinely_different_windows_still_differ(self) -> None:
        """The quantum must not be so coarse that distinct clips collide — that would serve the
        WRONG media, which is worse than a cache miss."""
        assert clips.clip_cache_path("ds--doc", 1.0, 9.0) != clips.clip_cache_path("ds--doc", 1.0, 12.0)
        assert clips.clip_cache_path("ds--doc", 1.0, 9.0) != clips.clip_cache_path("ds--other", 1.0, 9.0)


class TestTheBuildIsBounded:
    """A queue that grows without limit is the pool-exhaustion half of the finding."""

    def test_there_is_a_declared_concurrency_bound(self) -> None:
        assert hasattr(clips, "MAX_CONCURRENT_BUILDS"), (
            "clips.py declares no concurrency bound — every caller queues on one global lock and holds a threadpool thread while it waits"
        )
        assert clips.MAX_CONCURRENT_BUILDS >= 1

    def test_a_build_REFUSES_rather_than_queueing_when_saturated(self) -> None:
        """Refusing is the point. Queueing is what holds the thread; a 503 returns it immediately
        and tells the caller to retry, which is a fact they can act on."""
        assert hasattr(clips, "ClipBusyError"), (
            "there is no way for a saturated build to refuse — without one the only options are queue (holds a thread) or crash"
        )

    def test_the_bound_is_enforced_by_the_builder(self) -> None:
        """Pinned on the builder rather than the route: `build_clip` is the shared seam, and a second
        caller added later must inherit the bound rather than re-implement it."""
        saturated = [clips.acquire_build_slot() for _ in range(clips.MAX_CONCURRENT_BUILDS)]
        try:
            with pytest.raises(clips.ClipBusyError):
                clips.acquire_build_slot()
        finally:
            for slot in saturated:
                slot.release()


def test_the_media_module_declares_an_auth_dependency() -> None:
    """The module had none at all, while its siblings gate on the same corpus objects.

    Structural rather than a request test: the viewer's media routes need a real dataset registry to
    answer, and the property here is that the door EXISTS — a module with no auth import cannot be
    gated by any amount of configuration.
    """
    from pathlib import Path

    source = Path(clips.__file__).parent.parent / "api" / "v1" / "endpoints" / "media.py"
    text = source.read_text()
    assert "READ_DATA" in text, (
        "media.py imports no relation constant — the clip route serves media BYTES and its sibling `pages.py` gates the same class of read on can_read_data"
    )
    assert "CheckerDep" in text and "CurrentSubject" in text, "media.py binds no subject or checker"
