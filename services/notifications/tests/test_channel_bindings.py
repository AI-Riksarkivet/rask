"""What actually goes ONTO the wire for each channel.

`make_binding_sender` had NO test at all, which is how the Slack body survived: everything above it —
render, the claim ledger, the fan-out — was covered, so the plane looked tested while the one step
that talks to a provider was not.

The bug it hid: both channels were sent `data=body`, the plain-text render, on the argument that "the
HTTP binding ignores them and takes the body … a difference the Component already absorbs". It does
not absorb it. A Slack incoming webhook takes a JSON object and answers `invalid_payload` to a bare
string, so every Slack push failed — and failed PERMANENTLY, because `deliver_to_channels` claims
`(notification_id, channel)` BEFORE sending and deliberately does not roll the claim back. That
ordering is a ruling with its own reasoning (a re-send is the failure a person sees; the bell already
covers a missed push), so it stands; what was wrong is that the send could never succeed.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from notifications.api.channels import EMAIL, make_email_sender, make_push, make_slack_sender
from notifications.models import InboxPointer, NotificationReason


class _Binding:
    """The one method `Sender` needs, recording what it was handed and how many calls were in flight at once."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.most_in_flight = 0

    async def invoke_binding(self, *, binding_name: str, operation: str, data: str, binding_metadata: dict[str, str]) -> object:
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            # A sidecar call hands the loop to whatever else is ready, as this does.
            await asyncio.sleep(0)
            self.calls.append({"binding": binding_name, "operation": operation, "data": data, "metadata": dict(binding_metadata)})
        finally:
            self.in_flight -= 1
        return None


class _Inbox:
    """One subject's inbox as `make_push` reads it: an email preference, and a claim that holds."""

    def __init__(self, address: str) -> None:
        self.address = address

    async def get_prefs(self) -> dict[str, Any]:
        return {"channels": [EMAIL], "destinations": {EMAIL: self.address}, "digest_seconds": None}

    async def claim_channel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"claimed": True}


@pytest.mark.asyncio
async def test_the_smtp_sender_sends_the_render_as_escaped_html_with_the_smtp_metadata_keys() -> None:
    """The SMTP binding sends its data as `text/html` whatever it holds ([[CTL-30]]), so the body is the
    render as an HTML document: every `<`, `>`, `&` and quote an object id or a run carries is escaped,
    and each line of the render stays a line in the delivered mail."""
    binding = _Binding()
    send = make_email_sender(binding, binding="notifications-email", timeout_seconds=5)

    await send(
        destination="a@b.c",
        subject_line="silver$<b>pages</b> — Fail",
        body='silver$<b>pages</b> — Fail\n\nRun: run-1&co "x"\nReason: author',
    )

    call = binding.calls[0]
    assert call["operation"] == "create"
    assert call["data"] == (
        "<!doctype html>\n<html><body><p>"
        "silver$&lt;b&gt;pages&lt;/b&gt; — Fail<br>\n<br>\nRun: run-1&amp;co &quot;x&quot;<br>\nReason: author"
        "</p></body></html>\n"
    )
    assert call["metadata"]["emailTo"] == "a@b.c"
    assert call["metadata"]["subject"] == "silver$<b>pages</b> — Fail"


@pytest.mark.asyncio
async def test_concurrent_pushes_never_have_two_email_sends_in_flight_at_the_sidecar() -> None:
    """Measured against daprd 1.19.0-rc.4's `bindings.smtp`, concurrent invocations at one sidecar delivered
    messages to another send's recipient, and the same sends one at a time delivered none ([[CTL-30]]). The
    bus lane, the reconciler's tick and the digest drains all push through the one email sender a process
    builds, so that sender lets one send through at a time."""
    binding = _Binding()
    push = make_push(
        {EMAIL: make_email_sender(binding, binding="notifications-email", timeout_seconds=5)},
        open_inbox=lambda subject: _Inbox(f"{subject}@example.test"),
    )
    subjects = [f"person-{index}" for index in range(8)]

    await asyncio.gather(*(push(subject, _pointer_for(subject)) for subject in subjects))

    assert binding.most_in_flight == 1
    assert sorted(call["metadata"]["emailTo"] for call in binding.calls) == sorted(f"{subject}@example.test" for subject in subjects)


def _pointer_for(subject: str) -> dict[str, Any]:
    return InboxPointer(
        notification_id=f"run-{subject}@COMPLETE",
        reason=NotificationReason.AUTHOR,
        object_id=f"bronze${subject}",
        occurred_at=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_the_slack_sender_posts_the_render_escaped_as_slack_formatting_asks_in_a_json_object() -> None:
    """An incoming webhook takes a JSON object (a bare string is `invalid_payload`) and reads `<` and `>` in its
    text as a control sequence, a mention or a link, and `&` as an escape. The render interpolates an object id
    and a run id, both estate-supplied, so the text escapes those three as Slack's message formatting asks, and
    a quote or a backslash still leaves a body Slack can parse. The post names no target: the webhook is the
    Component's own `url`, the operator's one channel ([[CTL-34]])."""
    binding = _Binding()
    send = make_slack_sender(binding, binding="notifications-slack", timeout_seconds=5)

    await send(destination="", subject_line="x", body='silver$"<!channel>" & <https://x.example|y> — Fail\nline\\two')

    call = binding.calls[0]
    assert (call["operation"], call["metadata"]) == ("post", {"Content-Type": "application/json"})
    assert json.loads(call["data"]) == {"text": 'silver$"&lt;!channel&gt;" &amp; &lt;https://x.example|y&gt; — Fail\nline\\two'}
