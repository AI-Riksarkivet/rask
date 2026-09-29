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
