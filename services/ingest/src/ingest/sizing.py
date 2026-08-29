"""How a run partitions its writes — PER RUN, not per deployment.

These four numbers were `os.getenv` reads at module scope in `worker.py`, which means one
estate-wide value for every source the plane will ever ingest. The shapes genuinely differ, and not
by a little: a million 40 KB text records and ten thousand 20 MB page images want opposite fragment
settings, and a rate-limited IIIF endpoint wants a different fetch concurrency from an S3 bucket in
the same datacentre. A caller who knows their source is the only one who can pick well, and the only
lever they had was asking an operator to restart the deployment with different env.

So: the request carries them, the workflow threads them, the worker resolves them, and ANY unset
field falls back to the deployment default — an ordinary request is byte-identical to before.

## Why the row default is 1024 and not Lance's 1,000,000

Lance's own guidance is ~1M rows per fragment with 10-100 GB a reasonable size range
(`lance_docs/guide.md`, "Practical guidance for fragment sizing"), and it names the per-row write as
*the* ingestion anti-pattern. This plane's default is 1024 — three orders of magnitude under it. That
is a deliberate collision, not a misread:

  the ACK CONTRACT     a unit is held UNACKED from fetch until its fragment is on the store, so the
                       open batch is in-flight unacked work. JetStream stops delivering at
                       `max_ack_pending` (2048), so a row target above it DEADLOCKS the drain — the
                       worker waits for units the queue will not send until it acks units it will
                       not ack until the batch fills.

  LANCE'S TARGET       wants far larger fragments than any ack window can hold.

The ack contract wins, because losing it loses data and losing Lance's target only costs layout. The
resolution is downstream, not here: `services/maintenance` compacts, and its whole job is merging
small fragments toward a target size. The plane therefore writes ACK-BOUNDED fragments and lets
compaction make them LANCE-SIZED. Raising `fragment_rows` past the ack ceiling does not buy a bigger
fragment; it buys a stuck run — which is why `resolve()` refuses it rather than accepting a number
that reads as more throughput.

The byte ceiling is the other half, and it is what actually fires on media: 1024 twenty-megabyte
pages is a 20 GB fragment, at the top of Lance's own range, so `fragment_bytes` closes the batch
first on anything image-shaped. Rows are the ceiling for small records; bytes for large ones.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ingest.config import settings


#: The DEPLOYMENT defaults — what a request that says nothing gets. Read at call time, not import
#: time, so a test (and `kubectl set env`) can move them without reimporting the module. The values
#: and their variable names are declared once, on `IngestSettings`; these four are the plane's names
#: for them.
def default_fragment_rows() -> int:
    return settings().fragment_rows


def default_fragment_bytes() -> int:
    return settings().fragment_bytes


def default_fetch_batch() -> int:
    return settings().fetch_batch


def default_fetch_concurrency() -> int:
    return settings().fetch_concurrency


class IngestSizing(BaseModel):
    """The per-run overrides. Every field optional — unset means "use the deployment default"."""

    fragment_rows: int | None = Field(
        default=None,
        gt=0,
        description="rows accumulated before ONE fragment is written; must stay under the queue's max_ack_pending",
    )
    fragment_bytes: int | None = Field(
        default=None,
        gt=0,
        description="…or this many payload bytes, whichever comes first — the ceiling that actually fires on media",
    )
    fetch_batch: int | None = Field(default=None, gt=0, description="units pulled from the queue per fetch round-trip")
    fetch_concurrency: int | None = Field(
        default=None,
        gt=0,
        description="in-flight fetches against the SOURCE — a politeness ceiling for rate-limited endpoints",
    )


class ResolvedSizing(BaseModel):
    """The effective numbers, every field present. What the worker actually runs on."""

    fragment_rows: int
    fragment_bytes: int
    fetch_batch: int
    fetch_concurrency: int


class SizingRefused(ValueError):
    """A requested value the plane will not run — raised at ACCEPT, never mid-run."""


def resolve(requested: IngestSizing | None = None) -> ResolvedSizing:
    """Overlay the request on the deployment defaults, and REFUSE what would deadlock.

    The refusal is the point. `fragment_rows >= max_ack_pending` does not produce a bigger fragment,
    it produces a drain that waits forever on units JetStream will not deliver — a hang, with no
    error, at whatever hour the run reached that batch size. Accepting the number and failing later
    would make the request look honoured. `POST /v1/ingests` therefore turns this into a 400 with the
    ceiling named, which is a fix the caller can act on.
    """
    from ingest.queue import max_ack_pending

    req = requested or IngestSizing()
    rows = req.fragment_rows if req.fragment_rows is not None else default_fragment_rows()

    ceiling = max_ack_pending()
    if rows >= ceiling:
        raise SizingRefused(
            f"fragment_rows={rows} is at or above the queue's max_ack_pending ({ceiling}). "
            f"A batch is held unacked while it fills, so the drain would stop being delivered units and hang. "
            f"Use at most {ceiling - 1}, and let services/maintenance compact toward Lance's larger target."
        )

    return ResolvedSizing(
        fragment_rows=rows,
        fragment_bytes=req.fragment_bytes if req.fragment_bytes is not None else default_fragment_bytes(),
        fetch_batch=req.fetch_batch if req.fetch_batch is not None else default_fetch_batch(),
        fetch_concurrency=req.fetch_concurrency if req.fetch_concurrency is not None else default_fetch_concurrency(),
    )
