"""ANN-06 — a domain error must cross the sidecar as a STRUCTURED payload, not as prose.

`_translating` rebuilds an actor-side `IllegalTransition` on this side of the sidecar, which is
what turns a refused transition into a 409 carrying the reason. The transport is Dapr's, and Dapr
gives an exception exactly one channel: the actor-side FastAPI handler serialises `repr(ex)` into a
JSON envelope (`dapr/ext/fastapi/actor.py`), the SDK nests that inside its own message, and the
client sees a string.

These three cases are what a string reconstructed by regex costs, and every one of them is on a
live path:

* the estate's own refusal reasons contain an em dash (`machines.identity_violation`,
  `actor._refuse_if_frozen`), and `json.dumps` escapes it to `\\u2014`;
* an annotator id can carry backslashes (a UNC-shaped domain subject, `\\\\CORP\\dave`), and the
  hand-rolled un-escaper eats one of every pair;
* a failure that merely MENTIONS the class must stay the failure it is — misreading one as a
  refused transition answers 409 for what is a 500, and `saga._converge` reads exactly this
  exception type as "already converged", i.e. as SUCCESS.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from annotator.projects.machines import IllegalTransition
from annotator.projects.models import TaskState
from annotator.projects.proxies import _translating


def _over_the_sidecar(exc: BaseException) -> Exception:
    """What the client SDK raises for an actor-side `exc`, escaping and all.

    `dapr/ext/fastapi/actor.py` answers 500 with `repr(ex)` inside a JSON body; the SDK then nests
    that body inside its own message. Two `json.dumps` reproduce the two escaping layers the
    proxy's own docstring describes.
    """
    body = json.dumps({"errorCode": "ERR_ACTOR_INVOKE_METHOD", "message": repr(exc)})
    return RuntimeError(f"Dapr invocation failed: {json.dumps(body)}")


async def _round_trip(exc: BaseException) -> BaseException:
    """Raise `exc` from the far side of the proxy and hand back whatever reaches the caller."""

    async def call(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    try:
        await _translating(call)()
    except BaseException as reached:  # noqa: BLE001 - the assertion IS which exception reached us
        return reached
    raise AssertionError("the translating proxy swallowed the failure")


@pytest.mark.asyncio
async def test_a_refusal_reason_survives_the_hop_character_for_character() -> None:
    """The em dash in the estate's own refusal reasons must reach the 409, not `\\u2014`."""
    original = IllegalTransition("task", TaskState.CLAIMED, "release (the task is held by o'brien — releasing it needs can_manage)")

    reached = await _round_trip(_over_the_sidecar(original))

    assert isinstance(reached, IllegalTransition)
    assert reached.event == original.event
    assert reached.kind == "task"
    assert reached.state == "claimed"


@pytest.mark.asyncio
async def test_a_backslash_in_a_subject_is_not_eaten_by_the_unescaper() -> None:
    """A UNC-shaped annotator (`\\\\CORP\\dave`) keeps BOTH leading backslashes across the hop."""
    original = IllegalTransition("task", "claimed", "submit (the task is held by \\\\CORP\\dave)")

    reached = await _round_trip(_over_the_sidecar(original))

    assert isinstance(reached, IllegalTransition)
    assert reached.event == original.event


@pytest.mark.asyncio
async def test_a_failure_that_merely_names_the_class_is_not_rewritten_into_one() -> None:
    """Only a real `IllegalTransition` becomes one here.

    The consequence of getting this wrong is not cosmetic: `saga._converge` treats
    `IllegalTransition` as "the project is already where we were driving it", so an infrastructure
    failure rewritten into one is recorded as a publish that SUCCEEDED.
    """
    unrelated = RuntimeError("state store save failed while handling IllegalTransition bookkeeping")

    reached = await _round_trip(_over_the_sidecar(unrelated))

    assert not isinstance(reached, IllegalTransition)


@pytest.mark.asyncio
async def test_the_project_half_keeps_its_kind() -> None:
    """`kind` distinguishes a project refusal from a task one and must not default to `task`."""
    original = IllegalTransition("project", "frozen", "publish (tasks are not all terminal)")

    reached = await _round_trip(_over_the_sidecar(original))

    assert isinstance(reached, IllegalTransition)
    assert (reached.kind, reached.state, reached.event) == ("project", "frozen", original.event)
