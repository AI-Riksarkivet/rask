"""Channels — pushing a notification somewhere other than the bell.

**Not a parallel notifier.** Email rides the SAME subscriptions, the same audience and the same
visibility gate as the inbox; a channel is a per-user delivery preference on a decision that has
already been made. Building a second pipeline is how the two end up disagreeing about who was told.

**A channel reaches the person alone.** A push carries the row the visibility gate checked for its
subject, so it goes to an address that subject supplied and nowhere a second reader sees it: a shared
channel, Slack's operator webhook among them, would hand every reader of it a row nobody checked them
against ([[CTL-34]]).

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
import html
import logging
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from email.errors import HeaderParseError
from email.headerregistry import Address
from typing import Any, Final, Protocol

from notifications.models import InboxPointer


log = logging.getLogger(__name__)

#: Channel ids. Strings rather than an enum on the WIRE because a stored preference must survive a
#: channel being removed from the build — prefs are a set of ids, and the dispatch table is what
#: decides which of them can actually be sent.
EMAIL: Final = "email"

#: What this build can send. A stored preference naming another channel is kept (a channel removed from the build must
#: not make a stored document unreadable); the prefs door refuses an opt-in to one, and the door and the actor's write
#: both refuse a destination for one, so no address for a channel nobody sends is kept. Email alone: a channel reaches
#: the person and no second reader, so a shared one such as Slack is none ([[CTL-34]]).
KNOWN_CHANNELS: Final = frozenset({EMAIL})

#: RFC 5321's 256-octet path less its two angle brackets: the longest address a relay must take.
_MAX_ADDRESS_LENGTH: Final = 254
#: What the SMTP binding reads as a list of recipients (`;`), and what a header reads as one (`,`).
_RECIPIENT_SEPARATORS: Final = (";", ",")


def _is_one_bare_address(value: str) -> bool:
    """Whether `value` is exactly one addr-spec, the only email destination the channel sends to ([[CTL-30]]).

    The SMTP binding reads `emailTo` as a list split on `;` and sends to every address in it, and a
    destination it cannot parse fails every send. So a destination carries no separator, even inside a
    quoted local part the address grammar allows; no display name, comment or padding, which the
    stdlib parser reads back as a different string; no control or invisible format character, which
    the parser accepts in a domain; and no more than a relay must take.
    """
    if len(value) > _MAX_ADDRESS_LENGTH or any(separator in value for separator in _RECIPIENT_SEPARATORS):
        return False
    if any(_breaks_or_hides(character) for character in value):
        return False
    try:
        parsed = Address(addr_spec=value)
    except (ValueError, HeaderParseError):
        return False
    return parsed.addr_spec == value


def _breaks_or_hides(character: str) -> bool:
    """A control, format, surrogate, private-use or unassigned code point, or a line or paragraph separator."""
    category = unicodedata.category(character)
    return category[0] == "C" or category in {"Zl", "Zp"}


def destination_fault(destinations: Mapping[str, str]) -> str | None:
    """Why these destinations cannot be stored, or `None` when each is one its channel sends to.

    A destination is taken only for a channel in `KNOWN_CHANNELS`, so no webhook or other address for a channel this
    build does not send is kept in an inbox's state ([[CTL-34]]). An email destination is exactly one bare address
    ([[CTL-30]]).
    """
    unknown = sorted(set(destinations) - KNOWN_CHANNELS)
    if unknown:
        return f"unknown notification channel(s): {', '.join(unknown)}"
    email = destinations.get(EMAIL)
    if email and not _is_one_bare_address(email):
        return f"the {EMAIL} destination must be exactly one bare address: no list, display name or control character, at most 254 characters"
    return None


class DaprBindingClient(Protocol):
    """The one method this module needs off a Dapr client. Narrowed to it rather than typed `Any`
    because the concrete SDK client carries a hundred others, and the fake in the tests should have
    to implement one."""

    async def invoke_binding(self, *, binding_name: str, operation: str, data: str, binding_metadata: dict[str, str]) -> object: ...


class Sender(Protocol):
    """One channel's egress, to the address the subject supplied. A Protocol because there are two
    implementations, SMTP and the test fake, which is the bar `writing-python` sets for introducing one."""

    async def __call__(self, *, destination: str, subject_line: str, body: str) -> None: ...


def render(pointer: InboxPointer) -> tuple[str, str]:
    """The message, built from the POINTER alone.

    Deliberately spare. A notification says what happened and where to look; it is not the log, and an
    email that tried to be one would carry run detail this plane never re-checked. The object id and
    the run id are both already in the row the subject may see.
    """
    state = pointer.notification_id.rsplit("@", 1)[-1].upper() if "@" in pointer.notification_id else ""
    shown = pointer.object_name or pointer.object_id
    headline = f"{shown} — {state.title()}" if state else shown
    lines = [headline, ""]
    if pointer.source_run_id:
        lines.append(f"Run: {pointer.source_run_id}")
    lines.append(f"Reason: {pointer.reason}")
    lines.append(f"At: {pointer.occurred_at.isoformat()}")
    return headline, "\n".join(lines)


def _html_body(subject_line: str, body: str) -> str:
    """SMTP's wire body: the render as an HTML document, every line escaped. `subject_line` rides in the binding metadata.

    The SMTP binding sends its data as `text/html` whatever it holds ([[CTL-30]]), so a plain render loses
    its line breaks, and a `<`, `>` or `&` an object id or a run carries would be read as markup.
    """
    lines = "<br>\n".join(html.escape(line) for line in body.split("\n"))
    return f"<!doctype html>\n<html><body><p>{lines}</p></body></html>\n"


def make_email_sender(client: DaprBindingClient, *, binding: str, timeout_seconds: float) -> Sender:
    """The SMTP binding's `Sender`: `create`, the render as an escaped HTML body, and one send at a time.

    The binding is the seam on purpose: the SMTP host and its credentials live in a chart-managed
    Component reading the secret store, so no address and no token is ever in this service's env or its
    code. Swapping SMTP for a provider API is a Component change.

    ONE AT A TIME IS RASK'S CONTRACT WITH THE BINDING, not a tuning ([[CTL-30]]). Measured against daprd
    1.19.0-rc.4's `bindings.smtp`: concurrent invocations at one sidecar delivered messages to another
    send's recipient, and the same sends one at a time delivered none. The bus lane, the reconciler's tick
    and the digest drains can overlap, and all of them push through the one table `_channel_table` builds
    per process, and so through this sender, and a process has one sidecar. The lock waits outside the
    binding's timeout, so a send queued behind others still has all of its own.
    """
    one_at_a_time = asyncio.Lock()

    async def send(*, destination: str, subject_line: str, body: str) -> None:
        async with one_at_a_time:
            async with asyncio.timeout(timeout_seconds):
                await client.invoke_binding(
                    binding_name=binding,
                    operation="create",
                    data=_html_body(subject_line, body),
                    # The SMTP binding's own keys: `emailTo` names the recipient and `subject` the header.
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

    A failed send is logged and keeps its claim; the caller decides whether the aggregate is worth a RETRY.
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


def _defers(notification_id: str) -> bool:
    """Whether a digest may hold this notification back. Terminal failure never waits.

    Duplicated from `ChannelPrefs.digest_defers` rather than imported through it, because this path
    holds the prefs as a plain dict off the actor wire and constructing a model to ask one question
    would be the more indirect of the two. The rule is one line and it is stated in both places.
    """
    state = notification_id.rsplit("@", 1)[-1].upper() if "@" in notification_id else ""
    return state not in {"FAIL", "FAILED", "ABORT"}


def make_push(table: ChannelTable, *, open_inbox: Callable[[str], Any], defer: bool = True) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
    """The concrete `ChannelPush`: read a subject's prefs, push the pointer, claim as you go.

    `defer=False` is the DIGEST DRAIN's pusher, and it exists because the drain is a different caller
    with a different policy — not because a caller sometimes wants to skip a rule.

    `InboxActor.send_digest` drains the window and hands each pointer back to a pusher. Handed the
    deferring one, every pointer meets the exact conditions that deferred it in the first place
    (`digest_seconds` still set, `_defers()` still true for a non-failure) and is armed AGAIN: the
    window reopens, fires, re-arms, and the notification is never sent. Only the reminder ticking
    forever says anything is wrong. Terminal failures were never affected — `_defers` sends those
    immediately — which is why this survived: the notification people actually watch for worked.

    A construction-time policy rather than a per-call flag keeps `ChannelPush` a two-argument
    callable at every call site, so neither the fan-out nor the reconciler has to know the digest
    exists.

    Reads prefs PER DELIVERY rather than caching them, and that is the honest trade: a cache would
    keep mailing someone for its TTL after they turned email off, and "I unsubscribed and it kept
    sending" is the one channel failure that costs trust rather than time. The read is one actor call
    against the small partition, on a path that has already written a row.

    A subject who opted into nothing costs exactly one call and sends nothing — which is the common
    case, so the early return matters.
    """

    async def push(subject: str, payload: dict[str, Any]) -> None:
        inbox = open_inbox(subject)
        prefs = await inbox.get_prefs()
        channels = list(prefs.get("channels") or [])
        if not channels:
            return
        pointer = InboxPointer.model_validate(payload)

        # DIGEST: batch this one into the next window instead of sending it now — unless it is a
        # FAILURE, which always goes immediately. Digesting a failure to reduce the volume of
        # successes is the trade nobody wants made for them, and it is the one a naive digest makes
        # silently. The row is already written either way, so the bell is correct regardless.
        digest_seconds = prefs.get("digest_seconds")
        if defer and digest_seconds and _defers(pointer.notification_id):
            await inbox.arm_digest({"seconds": int(digest_seconds)})
            return

        async def claim(notification_id: str, channel: str) -> bool:
            result = await inbox.claim_channel({"notification_id": notification_id, "channel": channel})
            return bool(result.get("claimed"))

        sent = await deliver_to_channels(
            pointer,
            channels=channels,
            destinations=dict(prefs.get("destinations") or {}),
            table=table,
            mark_sent=claim,
        )
        if sent:
            # Counted by CHANNEL and OUTCOME only — never by subject. The subject is per-user data and
            # belongs on a span or a log line, never on a metric label, where it would make the series
            # cardinality the size of the user base.
            log.info("channel_pushed", extra={"channels": sent})

    return push
