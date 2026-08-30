"""A cached clip must survive being evicted while it is being served.

open_fastapi-audit — "A cached clip can be unlinked between the handler's `exists()` check and
Starlette's `os.stat`, turning a valid `/api/media-clip` request into a 500".

`build_clip` returns a path on a cache hit; the route hands that path to `FileResponse`, which stats
it when the response is sent. Between those two moments `evict_old_clips` can unlink it — the cache is
bounded at 50 and every build evicts — and the caller gets a 500 for a request that was valid and for
a file that existed when it was checked.

The fix is the one `file-handling.md` implies: pass the `stat_result` taken at check time. On Linux
the inode stays alive for an open handle, so the bytes are still served; only the second stat was
fatal.

THE TITLE'S SECOND CLAIM IS DROPPED, per the audit's own correction: `evict_old_clips` sorts by mtime
and drops the oldest-CREATED, which is FIFO, not "the hottest entry first". The real and smaller point
is that it is FIFO *because reads never touch mtime*, so a long-lived hot clip is eventually evicted
on age alone and pays another 120s-capped transcode. `os.utime` on a hit makes mtime mean "last
served" and turns the existing sort into a real LRU — no new bookkeeping.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from viewer.services import clips


def test_a_cache_hit_refreshes_mtime_so_the_sort_is_LRU(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the eviction is FIFO: a hot clip dies of age and pays another transcode."""
    monkeypatch.setattr(clips, "CACHE_DIR", tmp_path)
    out = tmp_path / "hot_0_4.mp4"
    out.write_bytes(b"mp4")
    old = time.time() - 10_000
    os.utime(out, (old, old))

    hit = clips.build_clip("src", "hot", 0.0, 1.0)
    assert hit == out
    assert out.stat().st_mtime > old, (
        "a cache HIT left mtime untouched, so `evict_old_clips` still sorts by creation time — a hot "
        "clip is evicted on age alone and rebuilt at 120s-capped ffmpeg cost"
    )


def test_eviction_keeps_the_recently_SERVED_not_the_recently_built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clips, "CACHE_DIR", tmp_path)
    now = time.time()
    for i in range(3):
        clip = tmp_path / f"c{i}_0_4.mp4"
        clip.write_bytes(b"mp4")
        os.utime(clip, (now - 100 + i, now - 100 + i))

    # c0 is the OLDEST by creation. Serve it, then evict down to two.
    clips.build_clip("src", "c0", 0.0, 1.0)
    clips.evict_old_clips(tmp_path, keep=2)

    survivors = {p.name for p in tmp_path.glob("*.mp4")}
    assert "c0_0_4.mp4" in survivors, "the clip just served was evicted — the sort is still FIFO"


def test_the_route_survives_an_unlink_between_check_and_send(tmp_path: Path) -> None:
    """The race itself: the file is gone by the time starlette stats it.

    A `FileResponse` given a `stat_result` does not stat again, so a concurrent eviction cannot turn a
    valid request into a 500. On Linux the inode outlives the unlink for an open handle, so the bytes
    are still served.
    """
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.testclient import TestClient

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"MP4BYTES")
    taken = clip.stat()

    app = FastAPI()

    @app.get("/clip")
    def serve() -> FileResponse:
        clip.unlink()  # the eviction, racing the send
        return FileResponse(clip, media_type="video/mp4", stat_result=taken)

    response = TestClient(app, raise_server_exceptions=False).get("/clip")
    assert response.status_code == 200, "an eviction between check and send became a 500"


def test_the_handler_passes_the_stat_it_already_took() -> None:
    """The route must actually use it — a helper nobody calls fixes nothing."""
    import inspect

    from viewer.api.v1.endpoints import media

    source = inspect.getsource(media.media_clip)
    code = "\n".join(line for line in source.split("\n") if not line.strip().startswith("#"))
    assert "stat_result" in code, (
        "the route hands FileResponse a bare path, so starlette stats it a second time and a concurrent eviction turns a valid request into a 500"
    )


def test_the_atlas_points_cache_is_LRU_too() -> None:
    """The same one-line defect, in the sibling the Fix also names.

    `evict_to_bounds` pops from the FRONT of a plain dict, and insertion order is its LRU proxy — but
    only if a HIT moves the key. Without that, a hot atlas payload is evicted on insertion age alone
    and rebuilt by a full scan+encode of the table, which is the expensive operation the cache exists
    to avoid.
    """
    import inspect

    from viewer.api.v1.endpoints import atlas

    source = inspect.getsource(atlas)
    code = "\n".join(line for line in source.split("\n") if not line.strip().startswith("#"))
    assert "move_to_end" in code or "cache.pop(key" in code, (
        "a points-cache HIT does not move the key, so `evict_to_bounds` drops the oldest-INSERTED rather than the least-recently-used"
    )
