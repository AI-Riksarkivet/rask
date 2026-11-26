"""What a bus door does with an event once its signature has been checked ([[XC-078]]).

Every bus door makes the same decision: the catalog's control lane and maintenance's arrival consumer in the lakehouse, and a client's door on
the lakehouse's events (a notifier's subscriptions to the control lane and the lineage feed). The decision has to catch
this package's own
exceptions, so it is written once here and each door only translates the verdict into its transport's answer.

The door hands `judge` the check as a callable already bound to its event, its signer policy and its key source, for example
`lambda: verify_control_signature(envelope, source=..., signers=..., delegators=...)`. Which signers an action allows is the
caller's policy and not this module's.

THE THREE MODES are the chart's `signing.doors`:

* OFF: the door acts and verifies nothing. A signature the event carries is ignored, and nothing may be admitted on one.
* OBSERVE: the door verifies, always acts, and reports what ENFORCE would have done. It is the soak between producers that
  sign and doors that refuse: a refusal is acknowledged and gone, so a door that enforced before every producer signed
  would destroy honest events without a failure anywhere to point at.
* ENFORCE: the door acts only on an event a listed signer's published key verifies. A refusal is final (the door acknowledges
  it, counts it and drives nothing); an unreadable key list says nothing about the signature, so the event is judged again later.

AN EXCEPTION THAT IS NEITHER A REFUSAL NOR AN OUTAGE PROPAGATES in every mode. The verifiers turn whatever an event provokes into
a refusal, so anything else is the door's own bug, and treating it as a refusal would acknowledge a good event away.

`judge` BLOCKS when the check does (a key read is a blocking call), so an async door runs it in a worker thread.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from lakehouse.events.signing import KeySourceUnavailableError, RefusalReason, SignatureError, VerifiedSignature


#: What OBSERVE reports when it could not read the keys a signature is checked against: the one outcome that is not a refusal
#: reason. A counter keyed on `observed` stays a handful of series, since the rest come from the closed `signing.RefusalReason`.
KEYS_UNAVAILABLE: Final = "keys_unavailable"
#: What OBSERVE reports it would have done: a refusal's reason, or :data:`KEYS_UNAVAILABLE`.
type Observed = RefusalReason | Literal["keys_unavailable"]


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

    `verified` is what the check established, set whenever it ran and the signature verified: a door that admits
    something on the signer's identity reads it from here, never from the event. `detail` is the check's own sentence
    for a refusal or an outage, in either mode: what was wrong with the signature, or why the keys could not be read. It
    quotes what the event claims, so a door bounds it before it logs or answers with it.
    """

    model_config = ConfigDict(frozen=True)

    act: bool
    retry: bool = False
    refused: RefusalReason | None = None
    observed: Observed | None = None
    verified: VerifiedSignature | None = None
    detail: str | None = None


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
        verified = verify()
    except SignatureError as exc:
        return DoorVerdict(act=True, observed=exc.reason, detail=str(exc)) if observing else DoorVerdict(act=False, refused=exc.reason, detail=str(exc))
    except KeySourceUnavailableError as exc:
        return DoorVerdict(act=True, observed=KEYS_UNAVAILABLE, detail=str(exc)) if observing else DoorVerdict(act=False, retry=True, detail=str(exc))
    return DoorVerdict(act=True, verified=verified)


__all__ = [
    "KEYS_UNAVAILABLE",
    "DoorMode",
    "DoorVerdict",
    "Observed",
    "judge",
]
