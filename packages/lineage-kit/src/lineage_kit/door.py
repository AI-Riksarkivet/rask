"""What a bus door does with an event once its signature has been checked ([[XC-078]]).

Five doors in three services make the same decision: medallion's `/bronze-arrival` and `/publication-arrival`, notifications'
`/control-events` and `/lineage-events`, and maintenance's `/maintenance-arrival`. The decision has to catch this package's own
exceptions, so it is written once here and each door only translates the verdict into its transport's answer.

The door hands `judge` the check as a callable already bound to its event, its signer policy and its key source, for example
`lambda: verify_control_signature(envelope, source=..., signers=..., delegators=...)`. Which signers an action allows is the
caller's policy and not the kit's.

THE THREE MODES are the chart's `signing.doors`:

* OFF: the door acts and verifies nothing. A signature the event carries is ignored, and nothing may be admitted on one.
* OBSERVE: the door verifies, always acts, and reports what ENFORCE would have done. It is the soak between producers that
  sign and doors that refuse: a refusal is acknowledged and gone, so a door that enforced before every producer signed
  would destroy honest events without a failure anywhere to point at.
* ENFORCE: the door acts only on an event a listed signer's published key verifies. A refusal is final (the door acknowledges
  it, counts it and drives nothing); an unreadable key list says nothing about the signature, so the delivery is retried.

AN EXCEPTION THAT IS NEITHER A REFUSAL NOR AN OUTAGE PROPAGATES in every mode. The verifiers turn whatever an event provokes into
a refusal, so anything else is the door's own bug, and treating it as a refusal would acknowledge a good event away.

`judge` BLOCKS when the check does (a key read is a blocking call), so an async door runs it in a worker thread.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from lineage_kit.signing import KeySourceUnavailableError, SignatureError, VerifiedSignature


#: What OBSERVE reports when it could not read the keys a signature is checked against: the one outcome that is not a refusal
#: reason. A counter keyed on `observed` stays a handful of series, since the rest come from the kit's closed `RefusalReason`.
KEYS_UNAVAILABLE: Final = "keys_unavailable"


class DoorMode(StrEnum):
    """How a door treats an event's signature: not at all, as a trial run, or as a condition of acting."""

    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


class DoorVerdict(BaseModel):
    """What the door does with the event, and what it reports.

    `act` is whether the door drives what the event asks for. `retry` asks the transport to deliver it again, set only when
    ENFORCE could not read the keys and so has no verdict on the signature. `refused` is the refusal reason, set only when
    ENFORCE refused the event. `observed` is what OBSERVE would have done instead of acting: the refusal reason, or
    `KEYS_UNAVAILABLE`; it is None when the signature verified, and always None outside OBSERVE.
    """

    model_config = ConfigDict(frozen=True)

    act: bool
    retry: bool = False
    refused: str | None = None
    observed: str | None = None


def judge(verify: Callable[[], VerifiedSignature], mode: DoorMode) -> DoorVerdict:
    """The verdict for one event: `verify` is its signature check, and it is not called at all when ``mode`` is OFF.

    ENFORCE acts only when `verify` returns; a `SignatureError` (an unsigned event included) is a refusal carrying its reason, and
    a `KeySourceUnavailableError` is a retry. OBSERVE always acts and reports the same two outcomes as `observed`. Any other
    exception propagates.
    """
    if mode is DoorMode.OFF:
        return DoorVerdict(act=True)
    observing = mode is DoorMode.OBSERVE
    try:
        verify()
    except SignatureError as exc:
        return DoorVerdict(act=True, observed=exc.reason) if observing else DoorVerdict(act=False, refused=exc.reason)
    except KeySourceUnavailableError:
        return DoorVerdict(act=True, observed=KEYS_UNAVAILABLE) if observing else DoorVerdict(act=False, retry=True)
    return DoorVerdict(act=True)
