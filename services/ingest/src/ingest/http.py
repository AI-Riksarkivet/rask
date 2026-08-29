"""ONE outbound connection pool for the whole ingest process.

Every outbound call in this plane used to build and discard its own client — `_fetch_http` did it
INSIDE the per-unit fetch, and `catalog_service` / `provenance` did it implicitly through the
module-level `httpx.post` / `httpx.get` helpers, which construct a client per call. The cost is not
theoretical at this plane's shape: a unit fetch is the hot path of a run, so a million-unit backfill
paid a million TCP handshakes (and a million TLS handshakes against an `https://` source) for a
million GETs. `fetch_concurrency` exists to OVERLAP those fetches, and overlapping connects instead
of transfers turns the ceiling into a lie that shows up only as an unexplained throughput cliff —
which `fetch.py`'s own docstring already warned about while doing exactly this.

`httpx.Client` is safe to share across threads, which is what makes one pool the right shape here:
`UriFetcher.fetch` runs each blocking fetch in `asyncio.to_thread`, and the workflow activities run
on the Dapr worker's own threads.

TIMEOUTS STAY WITH THE CALL SITE. They differ by an order of magnitude and for good reasons — 60s
for a unit fetch off a slow source, 30s for a catalog create that may provision storage, 2s for a
provenance read on a status endpoint an operator hits when something is already wrong — so the pool
carries connection policy and each caller passes its own `timeout=`. A pool-wide default would
silently give one of them somebody else's deadline.
"""

from __future__ import annotations

import atexit
from functools import lru_cache
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import httpx


#: Sized against the fetch ceiling rather than guessed. `RASK_INGEST_FETCH_CONCURRENCY` (default 8)
#: bounds in-flight fetches per worker and a pod may drain several chunks, so the pool has to hold
#: comfortably more than one chunk's worth or the ceiling becomes the pool's rather than the
#: caller's — a politeness limit enforced by the wrong thing.
MAX_CONNECTIONS = 100
MAX_KEEPALIVE_CONNECTIONS = 20

#: How long an idle connection is kept. Long enough that a status poll or a chunk's units reuse one,
#: short enough that a rolling upstream restart is not answered from a pool of dead sockets.
KEEPALIVE_EXPIRY_SECONDS = 30.0


@lru_cache(maxsize=1)
def shared_client() -> httpx.Client:
    """The process-wide pooled client. `cache_clear()` is the hook tests use to start from a fresh pool.

    REDIRECTS ARE OFF, which is httpx's own default and — more to the point — what every call site
    had before they shared a pool: the catalog and lineage doors were `httpx.post` / `httpx.get`. A
    pool that followed redirects would silently change them, and for a POST that is not a small
    change: a 301/302/303 is followed AS A GET, so a create would leave as a read. The one caller
    that genuinely needs redirects — a unit fetch off an arbitrary source URL — asks for them per
    request, exactly as it asks for its own timeout.
    """
    import httpx

    client = httpx.Client(
        limits=httpx.Limits(
            max_connections=MAX_CONNECTIONS,
            max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
            keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS,
        ),
    )
    # The pool outlives every request by design, so nothing else will close it. Without this the
    # process exits on open sockets and httpx warns about an unclosed client on the way out — the
    # same untidy shutdown the OpenFGA client's `dispose` exists to avoid.
    atexit.register(client.close)
    return client
