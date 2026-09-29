"""Nothing publishes to a subject on this stream that nothing consumes.

`signal_drained` did exactly that. It published to `ingest.run.{run}.drained` to wake a chunk
workflow suspended on an external event — a design that had ALREADY been replaced (the drain became
an activity, and its return value is how the workflow learns the chunk is done), but the publish was
never deleted.

That is not merely dead code. `ingest.run.*.drained` matches this stream's own `ingest.>` subject
filter, and the stream is WORK_QUEUE retention — where a message is removed only when it is ACKED.
With no consumer anywhere, every chunk of every run left one message on the stream forever.

The general shape is the danger: a publish with no reader looks like a working mechanism, so nobody
questions it, and on a work queue it silently accumulates. These tests pin the specific regression
and the general rule.
"""

from __future__ import annotations


def test_the_DLQ_stays_OFF_this_stream() -> None:
    """Poison units are meant to accumulate — that is the point of parking them — so their subject
    must NOT match `ingest.>`, or they would pile up on the WORK QUEUE and consume its budget."""
    from ingest.queue import DLQ_SUBJECT, SUBJECT_ROOT

    assert not DLQ_SUBJECT.startswith(f"{SUBJECT_ROOT}."), (
        f"DLQ_SUBJECT {DLQ_SUBJECT!r} is under {SUBJECT_ROOT!r}: parked units would land on the work "
        f"queue, unacked and permanent, instead of on their own stream"
    )
