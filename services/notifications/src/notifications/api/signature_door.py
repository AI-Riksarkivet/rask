"""The signature doors on the two bus routes: whether an event was written by the producer it names ([[XC-078]]).

The token on a delivery proves only that THIS app's sidecar delivered it, and every pod the bus lets publish reaches
that sidecar. So the author a run event stamps and the actor a control event stamps are claims until a signature
proves them. Lineage verifies the same signatures before it records an event (`docs/LINEAGE.md` § Signed events), and
this door is what keeps an event lineage refused from ringing a bell here.

WHAT IS VERIFIED, AND WHEN. Only an event the route would act on, and always the ARRIVED dict, the CloudEvent's data
exactly as delivered: a model dump grows the fields a schema defaults, and the producer signed what it wrote.

* `/lineage-events`: an event `notifiable()` would deliver, against the estate's signer sets (`RASK_EVENT_SIGNERS`,
  `RASK_EVENT_DELEGATORS`), as lineage verifies it.
* `/control-events`: an event whose action is in `NAMED_ACTIONS`, against the identities of the role
  `control_signer_role` names for its action and object (`RASK_CONTROL_SIGNER_ROLES`), so a valid signature from
  another service is no authority over the action. A role the chart did not render verifies against no identity, and
  its events are refused as `signer`. The annotator's task actions and the grants on its own projects need no
  signature (owner rulings R3 and R5); the policy states the residual.

An event the route acknowledges and ignores is not verified, so it costs no key read and moves no counter.

WHAT EACH MODE DOES (`RASK_SIGNATURE_DOORS`; the verdict is `lineage_kit.door.judge`'s):

* off: nothing is verified, and a signature an event carries is ignored.
* observe: the event is delivered as with off. One that enforce would refuse is counted in
  `notifications.signature.would_refuse` and logged, which is the evidence enforce is safe to turn on.
* enforce: a refused event is acknowledged (SUCCESS) and told to nobody, counted in `notifications.signature.refused`
  and logged. Never DROP: on a subscription with a dead-letter topic a DROP parks the event, and it would park again on
  every replay. Keys the sidecar cannot serve say nothing about the signature, so the delivery is retried.

The keys are read through this pod's sidecar (`published_key_fetch` over `RASK_SECRET_STORE`) by one
`lineage_kit.keys.PublishedKeys` per app, kept on `app.state` so its cache outlives a delivery. A check blocks on that
read, so it runs in the threadpool.
"""

import logging
from collections.abc import Callable
from typing import Final

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError

from lineage_kit.door import DoorMode, DoorVerdict, judge
from lineage_kit.keys import PublishedKeys
from lineage_kit.signing import VerifiedSignature, control_signature_of, signature_of, verify_control_signature, verify_signature
from notifications.api.control_events import NAMED_ACTIONS
from notifications.api.ingest import DAPR_RETRY, DAPR_SUCCESS
from notifications.api.lineage_events import LineageRunEvent, notifiable
from notifications.api.metrics import Door, Lane, Outcome, record_ingress, record_signature_refused, record_signature_would_refuse
from notifications.api.settings import IngressSettings
from service_kit.control_events import CatalogControlEvent, control_signer_role
from service_kit.governed.signing_key import published_key_fetch


log = logging.getLogger(__name__)

#: Bounds what an event's own text puts into a log line: the identity it claims and its id are the producer's words.
_MAX_LOGGED_CHARS: Final = 300


def _bounded(text: str | None) -> str | None:
    return text if text is None or len(text) <= _MAX_LOGGED_CHARS else f"{text[:_MAX_LOGGED_CHARS]}..."


def _published_keys(request: Request, settings: IngressSettings) -> PublishedKeys:
    """This app's key reader, built on first use and kept on the app so its cache outlives one delivery."""
    state = request.app.state
    keys: PublishedKeys | None = getattr(state, "published_keys", None)
    if keys is None:
        keys = state.published_keys = PublishedKeys(published_key_fetch(settings.secret_store))
    return keys


async def _answer(door: Door, check: Callable[[], VerifiedSignature], mode: DoorMode, *, identity: str | None, event_id: str) -> dict[str, str] | None:
    """Run ``check`` under ``mode``, count and log the verdict, and return what the route answers instead of acting, or None to act."""
    verdict: DoorVerdict = await run_in_threadpool(judge, check, mode)
    logged = {"door": door.value, "identity": _bounded(identity), "event_id": _bounded(event_id)}
    if verdict.observed is not None:
        record_signature_would_refuse(door, verdict.observed)
        log.warning("notifications_signature_would_refuse", extra={**logged, "reason": verdict.observed})
    if verdict.act:
        return None
    if verdict.refused is None:
        # A verdict that neither acts nor names a refusal is the keys being unreadable: nothing is known about the
        # signature, so the sidecar redelivers rather than this door losing an honest event.
        log.warning("notifications_signature_keys_unavailable", extra=logged)
        record_ingress(Lane.BUS, Outcome.RETRIED)
        return DAPR_RETRY
    record_signature_refused(door, verdict.refused)
    log.warning("notifications_signature_refused", extra={**logged, "reason": verdict.refused})
    record_ingress(Lane.BUS, Outcome.SIGNATURE_REFUSED)
    return DAPR_SUCCESS


async def screen_lineage_event(arrived: object, request: Request, settings: IngressSettings) -> dict[str, str] | None:
    """None when `/lineage-events` may act on ``arrived``; otherwise the answer the sidecar gets instead, the event told to nobody.

    ``arrived`` is the CloudEvent's data. An event `notifiable()` would not deliver (or that does not parse, which the
    route drops) is not verified.
    """
    mode = DoorMode(settings.signature_doors)
    if mode is DoorMode.OFF or not isinstance(arrived, dict):
        return None
    try:
        notice = notifiable(LineageRunEvent.model_validate(arrived))
    except (ValidationError, TypeError, ValueError):
        return None
    if notice is None:
        return None
    claim = signature_of(arrived)
    source = _published_keys(request, settings).for_event(claim.kid if claim else None)
    return await _answer(
        Door.LINEAGE_EVENTS,
        lambda: verify_signature(arrived, source=source, signers=settings.event_signers, delegators=settings.event_delegators),
        mode,
        identity=claim.identity if claim else None,
        event_id=notice.delivery.notification_id,
    )


async def screen_control_event(arrived: object, request: Request, settings: IngressSettings) -> dict[str, str] | None:
    """None when `/control-events` may act on ``arrived``; otherwise the answer the sidecar gets instead, the event told to nobody.

    ``arrived`` is the CloudEvent's data. The envelope is parsed only to read its action and object; the signature is
    checked over ``arrived`` itself. An action outside `NAMED_ACTIONS` (or an envelope that does not parse, which the
    route drops) is not verified, and neither is one the policy exempts.
    """
    mode = DoorMode(settings.signature_doors)
    if mode is DoorMode.OFF or not isinstance(arrived, dict):
        return None
    try:
        event = CatalogControlEvent.model_validate(arrived)
    except ValidationError:
        return None
    if event.action not in NAMED_ACTIONS:
        return None
    role = control_signer_role(event.action, event.object_id)
    if role == "exempt":
        return None
    signers = settings.control_signer_roles.get(role, frozenset())
    claim = control_signature_of(arrived)
    source = _published_keys(request, settings).for_event(claim.kid if claim else None)
    return await _answer(
        Door.CONTROL_EVENTS,
        lambda: verify_control_signature(arrived, source=source, signers=signers, delegators=settings.event_delegators),
        mode,
        identity=claim.identity if claim else None,
        event_id=event.event_id,
    )
