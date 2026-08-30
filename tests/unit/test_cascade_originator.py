"""The medallion cascade's identity chain: a stage that FAILS names the person whose batch it was.

THE GAP (register row 18). Measured on the live estate: of ~712 lineage runs the authors are
`data_eng` (246), `ray` (187), `analyst` (124) and `reconcile` (49) — chart ROLE LITERALS from
`chart/values.yaml` `medallion.movers[].author` — and `lance-medallion/embed_features` failed
repeatedly across 08-22 → 08-29 with nobody told. Per `.claude/skills/rask-notifications` trap 1 that
author is CORRECT and unaddressable: `author_subject()` reads `author.sub` and nothing else, so a FAIL
run authored `data_eng` addresses an inbox actor NAMED `data_eng`. Trap 2 says it is not fixable at
the mover either — `enforce_author` would overwrite a human there anyway. The sanctioned mechanism is
the ORIGINATOR riding beside the author, exactly as the `/train` chain carries it
(`tests/unit/test_train_originator.py`).

WHERE THE MEDALLION CHAIN BROKE. Links 1-2 already held: `/produce` keeps the verified sub, stamps it
as `lance.originator` on the bronze-write event, and `/bronze-arrival` copies it onto the
`medallion.bronze` trigger. The bronze→silver mover then threads `trigger.originator` onto every one
of its four FAIL emits. The break is the TIER BOUNDARY. Under one door a mover does not publish the
next stage's trigger — it publishes its output to the catalog, whose `table_published` control event
wakes the next hop through `/publication-arrival`. That hop carried `cascade_id` and dropped the
human, and the publication head then derived an originator from the control event's ACTOR — which for
a cascade publish is the mover's own service identity (`service-bronze-to-silver`, the sub the service
door mints). So the silver→gold FAIL run named a SERVICE as the person it was for: an inbox actor
named after a mover, which looks delivered and is not (trap 4 — an address must identify a person).

THE FIX IS THE `cascade_id` SHAPE, because that field lost the same hop for the same reason and its
carrier is already in these three files: the mover puts the human on the publish body, the catalog
echoes it onto `table_published`, the publication head reads it off `extra`. The catalog resolves the
ORIGINATOR once, at the choke point, because it is the only component that knows whether its caller
was a person or a service — `IDToken.service` is set by its own service door. The medallion's
actor-sniffing guess is gone with it.

NO AUDIENCE IS A LEGITIMATE ANSWER, and this suite pins it too: a publication driven by a service with
no human behind it (a reconcile, a backfill, a cron sweep) carries NO originator at all. Per the skill,
a state change naming nobody is undeliverable, not under-delivered — and an empty or role-shaped
address is strictly worse than silence.

Every assertion below is on a CARRIED FIELD or a DELIVERED ROW, never on an ack status: `notifiable()`
answers an event it cannot target with a SUCCESS ack, which is exactly how this gap hid.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

from catalog.api.v1.endpoints.publication import publication_extra, publication_originator
from medallion.core.config import MedallionSettings
from medallion.services import catalog_register, ingest_trigger, publication_trigger
from medallion.services.trigger_guards import StageTrigger
from notifications.api.ingest import ingest_run_event
from notifications.api.metrics import Lane
from notifications.api.visibility import Visibility
from notifications.models import NotificationReason
from notifications.proxies import TypedActorProxy
from service_kit.governed.oidc import IDToken


if TYPE_CHECKING:
    from collections.abc import Iterable


#: The person. One name across every link, so a break shows up as an absence rather than a mismatch.
HUMAN = "alice"

#: What the bronze→silver mover authenticates to the catalog AS (`chart/values.yaml`
#: `medallion.movers[].serviceIdentity`). This is the string that was reaching the inbox.
MOVER_IDENTITY = "service-bronze-to-silver"

OPEN = Visibility(client=None, enabled=False)


def _settings(**overrides: Any) -> MedallionSettings:
    values: dict[str, Any] = {
        "MEDALLION_TRANSFORM_ROUTES": {"silver": "medallion.silver"},
        "MEDALLION_OPERATION": "aggregate_gold",
        "MEDALLION_AUTHOR": "analyst",
    }
    values.update(overrides)
    return MedallionSettings.model_validate(values)


class _FakeDapr:
    """Records what was published; no sidecar, no broker."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_kw: Any) -> None:
        self.published.append({"pubsub": pubsub_name, "topic": topic_name, "data": json.loads(data)})


# ── links 1-2: the head keeps the human (already green — pinned so a regression is visible) ───────


def test_the_cascade_head_copies_the_human_off_the_bronze_write_event() -> None:
    """`/produce` stamps `lance.originator`; `/bronze-arrival` is the last place the batch's human
    exists before every later actor is a service. Green before this change; a regression here would
    make every link below unreachable."""
    dapr = _FakeDapr()
    settings = _settings(MEDALLION_BRONZE_NAMESPACE="bronze", MEDALLION_BRONZE_DATASET="bronze$events")
    event = {
        "data": {
            "eventType": "COMPLETE",
            "run": {"runId": "run-1", "facets": {"lance": {"operation": "seed_bronze", "token": "tok1", "originator": HUMAN}}},
            "outputs": [{"namespace": "bronze", "name": "bronze$events"}],
        }
    }

    assert asyncio.run(ingest_trigger.handle_bronze_arrival(cast(Any, dapr), settings, event)) == {"status": "SUCCESS"}
    assert dapr.published[0]["data"]["originator"] == HUMAN


# ── link 3: the mover hands the human to the catalog with its publish ─────────────────────────────


class _CapturingTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"table": "silver$features", "published": True, "from_version": 1, "to_version": 2, "assertions": []})


def _publish_stage_output(**kw: Any) -> _CapturingTransport:
    transport = _CapturingTransport()
    with httpx.Client(base_url="http://catalog", transport=transport) as client:
        catalog_register.publish_stage_output(
            catalog_url="http://catalog",
            table_id="silver$features",
            version=2,
            key_column="id",
            cascade_id="tok1",
            client=client,
            **kw,
        )
    return transport


def test_the_mover_puts_the_human_on_the_publish_it_sends_the_catalog() -> None:
    """The mover is the last holder of the batch's human before the tag move. It carried `cascade_id`
    across this boundary and dropped the person — the same hop, the same loss, one field apart."""
    transport = _publish_stage_output(originator=HUMAN)

    assert transport.bodies[0]["originator"] == HUMAN


def test_a_mover_with_no_human_behind_it_publishes_no_identity() -> None:
    """A blank is not an identity. `publication_originator` reads truthiness and an empty string
    would end up addressing an inbox actor named ``''``."""
    transport = _publish_stage_output()

    assert transport.bodies[0]["originator"] == ""


# ── link 4: the catalog resolves the originator ONCE, where it knows who its caller is ────────────


def _service_token(sub: str) -> IDToken:
    """What `catalog/api/security.py` mints for a caller that came through the SERVICE door."""
    return IDToken(iss="rask://service-door", sub=sub, aud="rask", iat=0, exp=60, service=True)


def _human_token(sub: str) -> IDToken:
    """What the OIDC verifier returns for a person — no `service` claim at all."""
    return IDToken(iss="https://idp.example", sub=sub, aud="rask", iat=0, exp=60)


def test_the_catalog_prefers_the_carried_human_over_the_service_that_published() -> None:
    """THE DEFECT, at its choke point. A cascade publish authenticates as the mover, so the actor is
    `service-bronze-to-silver`. The human on the body is the only person in the request."""
    assert publication_originator(HUMAN, _service_token(MOVER_IDENTITY)) == HUMAN


def test_a_service_publisher_with_no_carried_human_names_nobody() -> None:
    """A reconcile, a backfill or a cron sweep has no person behind it — the legitimate "no audience"
    answer. A service subject is not an address, and carrying it is worse than silence because it
    looks delivered."""
    assert publication_originator("", _service_token(MOVER_IDENTITY)) == ""


def test_a_person_who_publishes_by_hand_is_their_own_originator() -> None:
    """The UI path this head was built for: no cascade carried anything, and the actor IS the person."""
    assert publication_originator("", _human_token(HUMAN)) == HUMAN


@pytest.mark.parametrize(
    "claimed",
    [
        "*",
        "user:*",
        "team:acme#member",
        "user:team:acme#member",
        "  ",
        # THE ROLE LITERALS — omitted when this test was written, which is how the door shipped
        # accepting them. The movers author as `data_eng` / `analyst` and the producer as `ray`
        # (`chart/values.yaml` medallion.movers[].author), so these are not hypothetical inputs: they
        # are what the cascade actually sends. A door that let one through wrote a FAIL row into an
        # inbox actor NAMED after the role — the exact symptom this whole seam exists to remove, and
        # invisible because `notifiable()` acks an undeliverable event with SUCCESS.
        "data_eng",
        "analyst",
        "ray",
        "anon",
        "system",
        "service",
    ],
)
def test_a_claim_that_names_no_person_is_refused(claimed: str) -> None:
    """Trap 4: a wildcard is a statement about everyone and therefore about no one, and a userset
    addresses a group. Trap 1: a role literal is not an address either. Both are judged by the ONE
    definition in `catalog.core.lineage_emit.is_person_subject` — this door briefly kept a weakened
    local copy that omitted the role literals, and that is what let `data_eng` through.

    The claim rides an untrusted body, so the refusal lives beside the read."""
    assert publication_originator(claimed, _service_token(MOVER_IDENTITY)) == ""


def test_an_fga_spelled_principal_is_read_as_the_person_it_names() -> None:
    """Both spellings reach this field honestly — the medallion carries the bare sub
    `authorize_produce` returns, an FGA principal is written `user:<sub>`. Refusing one of them would
    drop a real person over a wire-shape difference, silently and with a 200."""
    assert publication_originator(f"user:{HUMAN}", _service_token(MOVER_IDENTITY)) == HUMAN


def test_a_person_cannot_redirect_the_notification_to_somebody_else() -> None:
    """A verified sub beats a body field. Honouring the claim for a HUMAN caller would let anyone who
    may publish put a row in a named colleague's inbox — the forgery `author_subject` exists to stop,
    one field over."""
    assert publication_originator("mallory", _human_token(HUMAN)) == HUMAN


class _NoProject:
    async def project_for(self, _segment: str) -> str | None:
        return None


def test_the_published_control_event_carries_the_human() -> None:
    """`extra` is the whole hand-off: the publication head reads nothing else off the event."""
    extra = asyncio.run(
        publication_extra(
            cast(Any, _NoProject()),
            ["silver", "features"],
            from_version=1,
            to_version=2,
            location="s3://lake/silver",
            cascade_id="tok1",
            originator=HUMAN,
        )
    )

    assert extra["originator"] == HUMAN


def test_the_published_control_event_omits_an_absent_human() -> None:
    """Omitted, never blank — the same rule `project` and `cascade_id` already follow, so a consumer
    can tell "nobody" from somebody named ``''``."""
    extra = asyncio.run(publication_extra(cast(Any, _NoProject()), ["silver", "features"], from_version=1, to_version=2, location="s3://lake/silver"))

    assert "originator" not in extra


# ── link 5: the publication head carries it onto the next tier's trigger ──────────────────────────


def _published_event(*, extra: dict[str, Any], actor: str) -> dict[str, Any]:
    return {
        "data": {
            "action": "table_published",
            "object_id": "table:silver$features",
            "event_id": "evt-1",
            "actor": actor,
            "extra": {"from_version": 1, "to_version": 2, "location": "s3://lake/silver", **extra},
        }
    }


def _next_trigger(*, extra: dict[str, Any], actor: str) -> dict[str, Any]:
    dapr = _FakeDapr()
    settings = _settings()
    assert asyncio.run(publication_trigger.handle_publication(dapr, settings, _published_event(extra=extra, actor=actor))) == {"status": "SUCCESS"}
    return cast(dict[str, Any], dapr.published[-1]["data"])


def test_the_next_tiers_trigger_names_the_human_not_the_mover_that_published() -> None:
    """THE ROW'S DEFECT, at the hop where it happened. The head used to read the event's ACTOR, so a
    cascade publish handed the silver→gold mover `originator: service-bronze-to-silver` — and every
    FAIL that mover emitted addressed an inbox named after a service."""
    trigger = _next_trigger(extra={"originator": HUMAN}, actor=f"user:{MOVER_IDENTITY}")

    assert trigger["originator"] == HUMAN


def test_a_publication_with_no_human_carries_no_originator_at_all() -> None:
    """The cron/backfill path stated explicitly: no person, no address, no row. The key is ABSENT so
    `StageTrigger.originator` is `None` rather than a name nobody answers to."""
    trigger = _next_trigger(extra={}, actor=f"user:{MOVER_IDENTITY}")

    assert "originator" not in trigger
    assert StageTrigger.model_validate(trigger).originator is None


# ── the delivery: the FAIL run reaches a named person's inbox ─────────────────────────────────────


class _Inbox:
    def __init__(self, plane: _Plane, subject: str) -> None:
        self._plane = plane
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = self._plane.boxes.setdefault(self._subject, [])
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class _Plane:
    """The inbox actors, as a dict. `InboxOpener` is a CALLABLE precisely so this needs no sidecar."""

    def __init__(self) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}

    def open(self, subject: str) -> TypedActorProxy:
        return cast(TypedActorProxy, _Inbox(self, subject))


def _gold_stage_fail(trigger: dict[str, Any]) -> dict[str, Any]:
    """The FAIL RunEvent the silver→gold mover emits, built exactly as `transform._emit_fail_run` does.

    Imported here rather than reimplemented: `build_run_event` is the one builder every medallion FAIL
    goes through, and `originator=trigger.originator or None` is verbatim what the four failing exits
    in `transform.py` pass it.
    """
    from medallion.schemas.events import build_run_event

    parsed = StageTrigger.model_validate(trigger)
    settings = _settings()
    return build_run_event(
        operation=settings.operation,
        author=settings.author,  # the chart ROLE LITERAL — correct, and unaddressable
        job_namespace=settings.job_namespace,
        inputs=[("silver", "silver$features")],
        output_namespace="gold",
        output_name="gold$catalog",
        token=parsed.token,
        cascade_id=parsed.cascade_id or None,
        project=parsed.project or None,
        originator=parsed.originator or None,
        event_type="FAIL",
        error_message="the gold aggregate raised",
    )


def _deliver(event: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    plane = _Plane()
    asyncio.run(ingest_run_event(event, lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open))
    return plane.boxes


def _reasons(rows: Iterable[dict[str, Any]]) -> set[str]:
    return {str(row["reason"]) for row in rows}


def test_a_failed_gold_stage_lands_a_row_in_the_humans_inbox() -> None:
    """THE WHOLE CHAIN, asserted on DELIVERED ROWS rather than on the ack — an event that names
    nobody acks SUCCESS too, which is exactly how this hid for 712 runs.

    `analyst` still gets a row as the AUTHOR, and that is correct: the literal is the truthful answer
    to "what ran this". What changed is that the person whose batch it was gets one too."""
    trigger = _next_trigger(extra={"originator": HUMAN, "cascade_id": "tok1"}, actor=f"user:{MOVER_IDENTITY}")

    boxes = _deliver(_gold_stage_fail(trigger))

    assert HUMAN in boxes, f"the failed cascade reached only {sorted(boxes)}"
    assert _reasons(boxes[HUMAN]) == {NotificationReason.ORIGINATOR.value}
    assert boxes[HUMAN][0]["object_id"] == "gold$catalog"


def test_the_failed_stage_never_writes_into_an_inbox_named_after_a_service() -> None:
    """The defect's own signature. Before this change the only non-author inbox this run touched was
    `service-bronze-to-silver` — a mover with no read state, no bell and no person behind it."""
    trigger = _next_trigger(extra={"originator": HUMAN, "cascade_id": "tok1"}, actor=f"user:{MOVER_IDENTITY}")

    boxes = _deliver(_gold_stage_fail(trigger))

    assert MOVER_IDENTITY not in boxes


def test_a_personless_cascade_failure_reaches_only_its_author() -> None:
    """No audience is an ANSWER, not a miss. A backfill nobody asked for delivers to the role that
    ran it and to nobody else — undeliverable rather than under-delivered."""
    trigger = _next_trigger(extra={"cascade_id": "tok1"}, actor=f"user:{MOVER_IDENTITY}")

    boxes = _deliver(_gold_stage_fail(trigger))

    assert set(boxes) == {"analyst"}
