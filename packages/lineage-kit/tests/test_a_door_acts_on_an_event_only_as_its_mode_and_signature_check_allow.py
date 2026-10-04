"""A door acts on an event only as its mode and what its signature check does allow ([[XC-078]]).

`judge` is the one place the off / observe / enforce decision is made for the five bus doors that verify a signature, so the
doors cannot come to differ on it. It is a pure function of the mode and of what the check does: verifies, refuses, cannot read
the keys, or fails for a reason of its own. Each of those is driven here with a check that does exactly that, in every mode
that treats it differently. What a door then does with the verdict (acknowledge, ask for a retry, count) is that door's own test.

Both ways of being wrong are pinned. A fail-open ENFORCE admits the forgery the signature exists to stop. A fail-closed OBSERVE
destroys honest events during the soak, where a refusal is acknowledged and gone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

import pytest

from lineage_kit import (
    DoorMode,
    DoorVerdict,
    KeySourceUnavailableError,
    SignatureRefusedError,
    UnsignedEventError,
    VerifiedSignature,
    judge,
)
from lineage_kit.door import KEYS_UNAVAILABLE


def _verifies() -> VerifiedSignature:
    return VerifiedSignature(identity="service-catalog", kid="0123456789abcdef")


def _is_unsigned() -> NoReturn:
    raise UnsignedEventError("the event carries no signature")


def _is_refused() -> NoReturn:
    raise SignatureRefusedError("'service-trainer' is not an identity that may sign", "signer")


def _cannot_read_the_keys() -> NoReturn:
    raise KeySourceUnavailableError("the public keys 'service-catalog' publishes could not be read")


def _fails_on_its_own() -> NoReturn:
    raise KeyError("a fault in the door, not in the event")


@pytest.mark.parametrize(
    ("mode", "check", "expected", "checks_run"),
    [
        # A check that would refuse is never run: an unenforced estate neither pays for a key read nor reports on it.
        pytest.param(DoorMode.OFF, _is_refused, DoorVerdict(act=True), 0, id="off-acts-and-never-runs-the-check"),
        pytest.param(DoorMode.ENFORCE, _verifies, DoorVerdict(act=True), 1, id="enforce-acts-on-a-verified-event"),
        pytest.param(DoorMode.ENFORCE, _is_unsigned, DoorVerdict(act=False, refused="unsigned"), 1, id="enforce-refuses-an-unsigned-event-and-says-why"),
        pytest.param(DoorMode.ENFORCE, _cannot_read_the_keys, DoorVerdict(act=False, retry=True), 1, id="enforce-retries-when-the-keys-cannot-be-read"),
        pytest.param(DoorMode.ENFORCE, _fails_on_its_own, KeyError, 1, id="enforce-lets-a-doors-own-fault-propagate"),
        pytest.param(DoorMode.OBSERVE, _verifies, DoorVerdict(act=True), 1, id="observe-acts-on-a-verified-event-and-reports-nothing"),
        pytest.param(DoorMode.OBSERVE, _is_refused, DoorVerdict(act=True, observed="signer"), 1, id="observe-acts-and-reports-the-refusal-it-would-have-made"),
        pytest.param(
            DoorMode.OBSERVE,
            _cannot_read_the_keys,
            DoorVerdict(act=True, observed=KEYS_UNAVAILABLE),
            1,
            id="observe-acts-and-reports-that-the-keys-were-unreadable",
        ),
        pytest.param(DoorMode.OBSERVE, _fails_on_its_own, KeyError, 1, id="observe-lets-a-doors-own-fault-propagate"),
    ],
)
def test_a_door_acts_on_an_event_only_as_its_mode_and_signature_check_allow(
    mode: DoorMode, check: Callable[[], VerifiedSignature], expected: DoorVerdict | type[Exception], checks_run: int
) -> None:
    runs: list[str] = []

    def verify() -> VerifiedSignature:
        runs.append("verify")
        return check()

    try:
        answered: DoorVerdict | type[Exception] = judge(verify, mode)
    except Exception as exc:
        answered = type(exc)

    assert answered == expected
    assert len(runs) == checks_run, f"the check ran {len(runs)} times"
