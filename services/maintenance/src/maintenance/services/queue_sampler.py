"""The worker's sampler of its work queue: every 15 s it reads off the bus what each stream holds and what each durable
consumer awaits (:class:`maintenance.services.queue.TaskQueue`'s reads) into the ``maintenance.queue.*`` gauges, which
the ``maintenance`` alert group reads (`deploy/alerting/rules.yml`)."""

import logging
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Final

from maintenance.core.metrics import record_consumer_backlog, record_consumer_unsampled, record_stream_depth, record_stream_unsampled
from maintenance.services.queue import QueueUnavailableError, TaskQueue
from maintenance.services.queue_wire import (
    ARRIVALS_CONSUMER,
    ARRIVALS_STREAM,
    DUE_CONSUMER,
    SCHEDULES_STREAM,
    STORE_EVENTS_CONSUMER,
    STORE_EVENTS_STREAM,
    TASK_CONSUMER,
    TASKS_STREAM,
)


log = logging.getLogger(__name__)

#: The streams the worker's sampler reads, each with whether its oldest message's age is read: a work queue whose backlog
#: ages (MAINTENANCE_TASKS drops a task at its 24 h max age; STORE_EVENTS holds an event until it is settled), never the
#: schedules, which hold a table's next time for as long as it is due.
SAMPLED_STREAMS: Final = ((TASKS_STREAM, True), (SCHEDULES_STREAM, False), (STORE_EVENTS_STREAM, True))
#: The durable consumers the sampler reads, each with its stream; the commit lane's joins them where the lane runs.
SAMPLED_CONSUMERS: Final = ((TASKS_STREAM, TASK_CONSUMER), (SCHEDULES_STREAM, DUE_CONSUMER), (STORE_EVENTS_STREAM, STORE_EVENTS_CONSUMER))
ARRIVALS_SAMPLED: Final = (ARRIVALS_STREAM, ARRIVALS_CONSUMER)
#: How often the worker samples them: six reads of the bus, a quarter of a minute apart, so a rule held for minutes reads
#: many samples of a backlog and not one.
SAMPLE_SECONDS: Final = 15.0


async def sample_queue(queue: TaskQueue, consumers: Collection[tuple[str, str]] = SAMPLED_CONSUMERS) -> int:
    """Read each sampled stream's depth and the backlog of each of ``consumers`` off the bus into their gauges; how many
    reads the bus did not answer. A read it did not answer leaves its gauge at its last value, logged and counted, so one
    stream's outage does not hide what the others say."""
    unread = 0
    for stream, aged in SAMPLED_STREAMS:
        try:
            state = await queue.stream_state(stream)
        except QueueUnavailableError as exc:
            log.warning("maintenance_queue_unsampled", extra={"stream": stream, "error": str(exc)[:300]})
            record_stream_unsampled(stream=stream, reads=1)
            unread += 1
            continue
        record_stream_unsampled(stream=stream, reads=0)
        record_stream_depth(stream=stream, messages=state.messages, oldest_seconds=state.oldest_seconds(datetime.now(UTC)) if aged else None)
    for stream, durable in consumers:
        try:
            backlog = await queue.consumer_state(stream, durable)
        except QueueUnavailableError as exc:
            log.warning("maintenance_queue_unsampled", extra={"consumer": durable, "error": str(exc)[:300]})
            record_consumer_unsampled(consumer=durable, reads=1)
            unread += 1
            continue
        record_consumer_unsampled(consumer=durable, reads=0)
        record_consumer_backlog(consumer=durable, waiting=backlog.num_pending, ack_pending=backlog.num_ack_pending, redelivered=backlog.num_redelivered)
    return unread
