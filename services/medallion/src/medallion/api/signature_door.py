"""The producer's two cascade heads act only on an event a signer their door allows has signed ([[XC-078]]).

Each head starts work in a tenant's name on what an event says: `/bronze-arrival` on a bronze-write RunEvent, and
`/publication-arrival` on the catalog's `table_published`. The app token proves only that this pod's sidecar delivered the
event, and any identity the bus lets publish on the topic can put an event there, so a head verifies the event's
`rask_signature` against the public keys its signer publishes before acting on it.

WHAT IS VERIFIED, AND WHEN. The event exactly as it arrived, at the route, once the head has decided it would act on it
and before it acts. An event the head acknowledges and ignores is never verified, so the rest of the topic's traffic
costs no key read. The bronze head admits an event signed by its own author from `RASK_EVENT_SIGNERS`, which the chart
renders as the producer and the catalog (the only identities that sign an event this head acts on), or by a delegator from
`RASK_EVENT_DELEGATORS` declaring the person it stamped. The publication head
admits only the identities of the role `service_kit.control_events.control_signer_role` names for `table_published`
(`RASK_CONTROL_SIGNER_ROLES`), so a valid signature from another service is no authority over a publication; a role the
chart does not render admits nobody.

THE VERDICT IS `lineage_kit.door.judge`'s, in the mode the chart renders (`RASK_SIGNATURE_DOORS`):

* OFF: the head acts and verifies nothing.
* OBSERVE: the head acts, and what an enforcing door would have refused is counted in `medallion.signature.would_refuse`
  and logged as `medallion_signature_would_refuse`.
* ENFORCE: a refusal is acknowledged (SUCCESS), counted in `medallion.signature.refused`, logged as
  `medallion_signature_refused`, and drives nothing. Never DROP: Dapr routes a DROP to the dead-letter topic, so the event
  would be parked, and parked again by every replay of the dead letters. A key list the store cannot serve says nothing
  about the signature, so that delivery is answered RETRY.

The public keys are read through this pod's sidecar (`published_key_fetch`) by one `PublishedKeys` per app, kept on
`app.state` so its cache outlives one delivery. A key read blocks, so the verdict is taken in a worker thread.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, Final, Literal

from fastapi import Request
from fastapi.concurrency import run_in_threadpool

from lineage_kit.door import DoorMode, judge
from lineage_kit.keys import PublishedKeys
from lineage_kit.signing import Signature, VerifiedSignature, control_signature_of, signature_of, verify_control_signature, verify_signature
from medallion.core.config import MedallionSettings
from medallion.core.metrics import record_signature_refused, record_signature_would_refuse
from service_kit.control_events import ControlAction, control_signer_role
from service_kit.governed.signing_key import published_key_fetch


log = logging.getLogger(__name__)

#: The routes that verify, as the `lance.medallion.door` label: a closed set, so the counters stay a handful of series.
type Door = Literal["bronze-arrival", "publication-arrival"]

_SUCCESS: Final = {"status": "SUCCESS"}
_RETRY: Final = {"status": "RETRY"}

#: Bounds what an event's own text puts on a log line: the claimed identity and the event id are whatever the sender wrote.
_MAX_LOGGED_CHARS: Final = 300


def _published_keys(request: Request, settings: MedallionSettings) -> PublishedKeys:
    """This app's key reader, built on first use and kept on the app. Built on the event loop, so two first deliveries share one."""
    state = request.app.state
    keys: PublishedKeys | None = getattr(state, "published_keys", None)
    if keys is None:
        keys = state.published_keys = PublishedKeys(published_key_fetch(settings.dapr_secret_store))
    return keys


def _bounded(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    return text if len(text) <= _MAX_LOGGED_CHARS else f"{text[:_MAX_LOGGED_CHARS]}..."


def _run_id(arrived: Mapping[str, Any]) -> object:
    run = arrived.get("run")
    return run.get("runId") if isinstance(run, dict) else None


async def withhold_lineage_event(request: Request, settings: MedallionSettings, *, door: Door, arrived: Mapping[str, Any]) -> dict[str, str] | None:
    """The answer to give instead of acting on a lineage event, or None when the head acts on it.

    ``arrived`` is the CloudEvent's data as it was delivered, never a re-serialisation: the producer signed the document it
    wrote.
    """
    if settings.signature_doors == "off":
        return None
    claim = signature_of(arrived)

    def verify(source: PublishedKeys) -> VerifiedSignature:
        return verify_signature(
            arrived, source=source.for_event(claim.kid if claim else None), signers=settings.event_signers, delegators=settings.event_delegators
        )

    return await _withhold(request, settings, door=door, claim=claim, event_id=_run_id(arrived), verify=verify)


async def withhold_control_event(
    request: Request, settings: MedallionSettings, *, door: Door, arrived: Mapping[str, Any], action: ControlAction, object_id: str
) -> dict[str, str] | None:
    """The answer to give instead of acting on a control event, or None when the head acts on it.

    The signers are the identities of the role whose signature ``action`` on ``object_id`` needs, and none when the chart
    renders no such role, so the event is refused as unsigned or as a signer no role admits rather than failing the
    delivery. An exempt action is acted on unverified.
    """
    if settings.signature_doors == "off":
        return None
    role = control_signer_role(action, object_id)
    if role == "exempt":
        return None
    signers = settings.control_signer_roles.get(role, frozenset())
    claim = control_signature_of(arrived)

    def verify(source: PublishedKeys) -> VerifiedSignature:
        return verify_control_signature(arrived, source=source.for_event(claim.kid if claim else None), signers=signers, delegators=settings.event_delegators)

    return await _withhold(request, settings, door=door, claim=claim, event_id=arrived.get("event_id"), verify=verify)


async def _withhold(
    request: Request,
    settings: MedallionSettings,
    *,
    door: Door,
    claim: Signature | None,
    event_id: object,
    verify: Callable[[PublishedKeys], VerifiedSignature],
) -> dict[str, str] | None:
    """Judge one event in the configured mode, report what the mode reports, and translate the verdict for the transport."""
    keys = _published_keys(request, settings)
    verdict = await run_in_threadpool(judge, lambda: verify(keys), DoorMode(settings.signature_doors))
    about = {"door": door, "event_id": _bounded(event_id), "identity": _bounded(claim.identity) if claim else None}
    if verdict.observed is not None:
        record_signature_would_refuse(door, verdict.observed)
        log.warning("medallion_signature_would_refuse", extra={**about, "reason": verdict.observed})
    if verdict.refused is not None:
        record_signature_refused(door, verdict.refused)
        log.warning("medallion_signature_refused", extra={**about, "reason": verdict.refused})
        return _SUCCESS
    if verdict.retry:
        log.warning("medallion_signature_keys_unavailable", extra=about)
        return _RETRY
    return None
