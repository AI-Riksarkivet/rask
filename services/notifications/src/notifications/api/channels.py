"""Channels — pushing a notification somewhere other than the bell.

**Not a parallel notifier.** Email and Slack ride the SAME subscriptions, the same audience and the
same visibility gate as the inbox; a channel is a per-user delivery preference on a decision that has
already been made. Building a second pipeline is how the two end up disagreeing about who was told.

**Composed from the pointer, never from the bus payload.** The claim-check invariant crosses a
channel boundary too: what goes in an email is built from the row this plane already decided a
subject may see. Nothing is read off the event body on the way out — an email is exactly as
disclosing as the inbox row it announces, which is what makes "your run failed" safe to send to an
address that is not governed by anything.

**Idempotent by `(event_id, subject, channel)`**, with the first two implied by where the ledger is
stored: the pointer on that subject's actor. JetStream is at-least-once, so without the check a
redelivered event emails you twice — and unlike a duplicated inbox row, which the actor collapses
silently, a duplicated email is a thing a person sees.

**No Dapr Workflow.** Nothing here mints an id mid-saga, so the engine is not earned: the estate's own
reopen-signal is "a multi-step path where re-running step 1 on crash is bad", and this is one step
with an idempotency key.

**Dispatch is a dict of callables**, not a Strategy hierarchy — the `writing-python` rule, and it
earns itself here: adding a channel is a table entry plus a sender, and nothing about the fan-out
changes.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from notifications.models import InboxPointer


log = logging.getLogger(__name__)

#: Channel ids. Strings rather than an enum on the WIRE because a stored preference must survive a
#: channel being removed from the build — prefs are a set of ids, and the dispatch table is what
#: decides which of them can actually be sent.
EMAIL: Final = "email"
SLACK: Final = "slack"


class DaprBindingClient(Protocol):
    """The one method this module needs off a Dapr client. Narrowed to it rather than typed `Any`
    because the concrete SDK client carries a hundred others, and the fake in the tests should have
    to implement one."""

    async def invoke_binding(self, *, binding_name: str, operation: str, data: str, binding_metadata: dict[str, str]) -> object: ...


class Sender(Protocol):
    """One channel's egress. A Protocol because there are genuinely three implementations — SMTP,
    Slack, and the test fake — which is the bar `writing-python` sets for introducing one."""

    async def __call__(self, *, destination: str, subject_line: str, body: str) -> None: ...


def render(pointer: InboxPointer) -> tuple[str, str]:
    """The message, built from the POINTER alone.

    Deliberately spare. A notification says what happened and where to look; it is not the log, and an
    email that tried to be one would carry run detail this plane never re-checked. The object id and
    the run id are both already in the row the subject may see.
    """
    state = pointer.notification_id.rsplit("@", 1)[-1].upper() if "@" in pointer.notification_id else ""
    headline = f"{pointer.object_id} — {state.title()}" if state else pointer.object_id
    lines = [headline, ""]
    if pointer.source_run_id:
        lines.append(f"Run: {pointer.source_run_id}")
    lines.append(f"Reason: {pointer.reason}")
    lines.append(f"At: {pointer.occurred_at.isoformat()}")
    return headline, "\n".join(lines)


def make_binding_sender(client: DaprBindingClient, *, binding: str, operation: str, timeout_seconds: float) -> Sender:
    """A `Sender` over a Dapr OUTPUT BINDING.

    The binding is the seam on purpose: the SMTP host, the Slack webhook URL and their credentials
    live in a chart-managed Component reading the secret store, so no address and no token is ever in
    this service's env or its code. Swapping SMTP for a provider API is a Component change.
    """

    async def send(*, destination: str, subject_line: str, body: str) -> None:
        async with asyncio.timeout(timeout_seconds):
            await client.invoke_binding(
                binding_name=binding,
                operation=operation,
                data=body,
                # `emailTo`/`subject` are the SMTP binding's own metadata keys; the HTTP binding
                # ignores them and takes the body. One shape, because the alternative is a per-channel
                # branch here for a difference the Component already absorbs.
                binding_metadata={"emailTo": destination, "subject": subject_line},
            )

    return send


type ChannelTable = dict[str, Sender]


async def deliver_to_channels(
    pointer: InboxPointer,
    *,
    channels: list[str],
    destinations: dict[str, str],
    table: ChannelTable,
    mark_sent: Callable[[str, str], Awaitable[bool]],
) -> list[str]:
    """Push one pointer to each channel the subject opted into. Returns the channels actually sent.

    CHECK-BEFORE-SEND, not send-then-record: `mark_sent` claims the `(notification_id, channel)` pair
    and answers False if it was already claimed. Recording afterwards would leave the window that
    matters — a crash between the send and the write re-sends on redelivery, which is the duplicate a
    person sees.

    BATCH WITH PARTIAL FAILURE: one channel's fault never aborts the others, because they are
    independent destinations and a Slack outage must not stop the email. Each failure is logged and
    counted; the caller decides whether the aggregate is worth a RETRY.
    """
    sent: list[str] = []
    for channel in channels:
        sender = table.get(channel)
        destination = destinations.get(channel)
        if sender is None or not destination:
            # An opted-into channel with no destination, or one this build does not ship. Not an
            # error: a preference must survive both, and the honest behaviour is to skip quietly.
            continue
        if not await mark_sent(pointer.notification_id, channel):
            continue
        subject_line, body = render(pointer)
        try:
            await sender(destination=destination, subject_line=subject_line, body=body)
        except Exception:
            # The claim stands rather than being rolled back, and that is the deliberate direction:
            # under at-least-once, re-sending is the failure a person SEES, while a missed push is one
            # the bell already covers — the inbox row is written either way.
            log.exception("channel_send_failed", extra={"channel": channel})
            continue
        sent.append(channel)
    return sent
