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

import json
from typing import Any

import pytest

from notifications.api.channels import make_binding_sender, make_slack_sender


class _Binding:
    """The one method `Sender` needs, recording what it was handed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def invoke_binding(self, *, binding_name: str, operation: str, data: str, binding_metadata: dict[str, str]) -> object:
        self.calls.append({"binding": binding_name, "operation": operation, "data": data, "metadata": dict(binding_metadata)})
        return None


@pytest.mark.asyncio
async def test_the_smtp_sender_carries_the_plain_body_and_the_smtp_metadata_keys() -> None:
    """Today's behaviour, pinned — the Slack fix must not disturb the channel that worked."""
    binding = _Binding()
    send = make_binding_sender(binding, binding="notifications-email", operation="create", timeout_seconds=5)

    await send(destination="a@b.c", subject_line="silver$pages — Fail", body="silver$pages — Fail\n\nReason: author")

    call = binding.calls[0]
    assert call["operation"] == "create"
    assert call["data"] == "silver$pages — Fail\n\nReason: author"
    assert call["metadata"]["emailTo"] == "a@b.c"
    assert call["metadata"]["subject"] == "silver$pages — Fail"


@pytest.mark.asyncio
async def test_the_slack_sender_sends_a_json_object_a_webhook_will_accept() -> None:
    binding = _Binding()
    send = make_slack_sender(binding, binding="notifications-slack", timeout_seconds=5)

    await send(destination="https://hooks.example/x", subject_line="silver$pages — Fail", body="silver$pages — Fail\n\nReason: author")

    call = binding.calls[0]
    assert call["operation"] == "post"
    payload = json.loads(call["data"])
    assert payload["text"] == "silver$pages — Fail\n\nReason: author", "Slack takes {'text': …}; a bare string is invalid_payload"
    assert call["metadata"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_the_slack_body_is_valid_json_even_when_the_render_carries_quotes() -> None:
    """The render is interpolated from an object id and a run id, both estate-supplied. Building the
    JSON by hand would be one f-string away from an unparseable body for a table named with a quote."""
    binding = _Binding()
    send = make_slack_sender(binding, binding="notifications-slack", timeout_seconds=5)

    await send(destination="https://hooks.example/x", subject_line="x", body='silver$"weird" — Fail\nline\\two')

    assert json.loads(binding.calls[0]["data"])["text"] == 'silver$"weird" — Fail\nline\\two'
