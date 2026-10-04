"""`request_approval` is the SOLE producer of `promotion_review_requested`, and it never ran.

The reason that asks a named person to decide a held promotion had no test executing it: the
`CatalogControlEvent` construction, the `_publish()` closure and the success log were all reported
missing by coverage. Two suites appeared to cover it and each covered the other half —
`test_promotion_review.py` stubs the activity, so the orchestration around it is proven and the thing
that names the human is not.

That matters more here than for an ordinary emit, because `extra["subject"]` IS the targeting.
`.claude/skills/rask-notifications` states it for the whole control lane: "`named_subject` returns
`None` for a missing subject, a bare `user:`, and the `*` wildcard, and the event is then filed IGNORED
with a SUCCESS ack." So every way of getting this field wrong produces a healthy-looking event that
reaches nobody — there is no failure signal anywhere downstream to catch it.

The workflow's own comment states the contract this file pins: "ASK BEFORE WAITING, and treat an
unsendable ask as a refusal: parking on an event nobody was told about is an outage wearing a pause."
So the return value is load-bearing in both directions — `False` must BLOCK rather than park.

THE ASK LEAVES SIGNED AS THE PRODUCER, OR NOT AT ALL ([[XC-078]]). An enforcing door acknowledges an unsigned
`promotion_review_requested` and drops it, so the approver is never told and the promotion waits out its window. The
producer hosts the workflow and signs as itself, and while its key is unresolved the activity raises, so the
workflow's own retry runs it again rather than blocking the promotion on an ask that never left. The key reaches the
holder the way it does in the cluster, through the sidecar's secret API (respx), and the ask is verified the way the
notifications door verifies it: `verify_control_signature` against the producer's published key.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
import respx
from dapr.ext.workflow import WorkflowActivityContext
from fastapi import FastAPI

from lineage_kit import PublishedKeys, SignatureError, VerifiedSignature, verify_control_signature
from medallion import workflow as wf
from medallion.core import lineage_publish
from medallion.core.config import MedallionSettings
from service_kit.governed.fga import canonical_object_id
from service_kit.governed.signing_key import SigningKeyUnavailableError


PRODUCER = "service-medallion-producer"
SECRETS = "http://localhost:3500/v1.0/secrets/lance-secrets"


class _StubActivityContext:
    """An activity context. `request_approval` never touches it — pinned by this file passing."""


def _ctx() -> WorkflowActivityContext:
    """The stub, typed as the real context.

    A `cast` rather than a subclass or a `  # `: `WorkflowActivityContext` takes a live
    workflow instance to construct, the activity provably never touches the parameter, and `ty` does
    not honour `type: ignore` anyway — it is another tool's syntax. This is the same shape
    `test_producer_targeting_contract.py` uses for its unused resolved dependencies.
    """
    return cast(WorkflowActivityContext, _StubActivityContext())


def _spec(**overrides: Any) -> dict[str, Any]:
    """A chart lane's hold for tenant `acme`: the stage runner has already qualified all four names."""
    base = {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "acme-silver",
        "from_dataset": "acme-silver$features",
        "to_namespace": "acme-gold",
        "to_dataset": "acme-gold$catalog",
        "operation": "aggregate_gold",
        "author": "analyst",
        "version": 7,
        "reasons": ["row_count_drop"],
        "approver": "alice",
        "approval_hours": 24,
    }
    return {**base, **overrides}


def _catalog_table_object(table_id: str) -> str:
    """The object the CATALOG grants on for this table: `table:<canonical catalog id>`, never re-spelled."""
    return f"table:{canonical_object_id(table_id.split('$'), delimiter='$')}"


def _bus(monkeypatch: pytest.MonkeyPatch, *, refuses: bool = False) -> list[dict[str, Any]]:
    """What reaches the bus, without one: each publish's arguments, or a bus that refuses every publish.

    Both the client and the publish helper are imported INSIDE the activity body, so they are patched
    on their defining modules rather than on `medallion.workflow` — patching the latter would bind
    nothing and the test would pass while the real client ran.
    """
    from dapr.aio import clients as dapr_clients

    import service_kit.dapr_publish as dapr_publish

    sent: list[dict[str, Any]] = []

    class _Client:
        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    async def _publish(_client: object, **kwargs: Any) -> None:
        if refuses:
            raise RuntimeError("pubsub unavailable")
        sent.append(kwargs)

    monkeypatch.setattr(dapr_clients, "DaprClient", _Client)
    monkeypatch.setattr(dapr_publish, "publish_event", _publish)
    return sent


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what reaches the bus, without one."""
    return _bus(monkeypatch)


@asynccontextmanager
async def _producer_key(monkeypatch: pytest.MonkeyPatch, pair: Any, *, readable: bool) -> AsyncIterator[None]:
    """The producer's key holder, installed as its lifespan installs it, resolving through the sidecar's secret API.

    Unreadable, the store will not give the seed, so the holder never reads the published list and holds no key.
    """
    monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
    respx.get(f"{SECRETS}/signing-key-{PRODUCER}").mock(return_value=httpx.Response(200, json={"seed": pair.seed}) if readable else httpx.Response(500))
    if readable:
        respx.get(f"{SECRETS}/signing-public-{PRODUCER}").mock(return_value=httpx.Response(200, json={"keys": pair.public}))
    settings = MedallionSettings.model_validate(
        {"MEDALLION_SECRETS_FROM_DAPR": True, "RASK_SIGNING_IDENTITY": PRODUCER, "MEDALLION_FGA_SERVICE_IDENTITY": PRODUCER}
    )
    holder = await lineage_publish.start_signing(FastAPI(), settings)
    try:
        yield
    finally:
        await lineage_publish.stop_signing(holder)


def _signed_as(event: dict[str, Any], pair: Any) -> VerifiedSignature | str:
    """Who the door finds signed ``event``: the producer's role, the estate's delegators, its published key. Else the refusal reason."""
    keys = PublishedKeys(lambda _secret: {"keys": pair.public})
    try:
        return verify_control_signature(event, source=keys.for_event(pair.kid), signers=frozenset({PRODUCER}), delegators=frozenset({"service-catalog"}))
    except SignatureError as exc:
        return exc.reason


@respx.mock
@pytest.mark.asyncio
async def test_the_ask_names_the_approver_and_is_signed_as_the_producer(
    published: list[dict[str, Any]], event_signer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q6's rule: `extra.subject` is the WORKER — here, the person being asked to decide."""
    pair = event_signer(PRODUCER)
    async with _producer_key(monkeypatch, pair, readable=True):
        asked = wf.request_approval(_ctx(), wf.PromotionSpec.model_validate(_spec()))

    assert asked is True
    assert len(published) == 1, f"expected exactly one control event, got {len(published)}"
    event = json.loads(published[0]["data"])

    assert event["action"] == "promotion_review_requested"
    assert event["extra"]["subject"] == "user:alice", (
        "the subject is the entire targeting for the control lane; anything but `user:<sub>` is filed IGNORED with a SUCCESS ack and reaches nobody"
    )
    assert event["object_id"] == _catalog_table_object("acme-gold$catalog"), (
        "the object must be the table the catalog grants on: any other spelling counts every recipient "
        "HIDDEN, so the audience is computed correctly and then discarded whole"
    )
    assert event["extra"]["reasons"] == ["row_count_drop"]
    assert event["extra"]["token"] == "tok-1", "the token is how the approver's decision finds this hold"
    assert _signed_as(event, pair) == VerifiedSignature(identity=PRODUCER, kid=pair.kid), "the door refuses an ask its producer did not sign"


def test_a_DECLARED_lane_ask_targets_the_table_the_catalog_knows(published: list[dict[str, Any]]) -> None:
    """A lane declared through the catalog door may name tenant-free ids (`curated$catalog`), and the
    stage resolves exactly those. The ask must gate the approver on THAT table: re-qualifying it names
    `table:acme-curated$catalog`, an object no grant mentions, so every approver is counted HIDDEN."""
    spec = _spec(from_namespace="landing", from_dataset="landing$events", to_namespace="curated", to_dataset="curated$catalog")

    assert wf.request_approval(_ctx(), wf.PromotionSpec.model_validate(spec)) is True

    assert json.loads(published[0]["data"])["object_id"] == _catalog_table_object("curated$catalog")


def test_no_approver_refuses_the_ask_instead_of_publishing_one_nobody_can_answer(published: list[dict[str, Any]]) -> None:
    """`approver` empty means nobody can be asked. The spec's own comment: that BLOCKS, never promotes."""
    assert wf.request_approval(_ctx(), wf.PromotionSpec.model_validate(_spec(approver=""))) is False
    assert published == [], "an unapprovable promotion must publish nothing at all"


def _outcome(ask: Callable[[], bool]) -> object:
    """What the activity hands the workflow: its return value, or the type of the missing key it raised."""
    try:
        return ask()
    except SigningKeyUnavailableError as exc:
        return type(exc)


@respx.mock
@pytest.mark.parametrize(
    ("cause", "outcome"),
    [
        pytest.param("the-bus-refuses-it", False, id="a-failed-publish-is-a-refusal-that-blocks"),
        pytest.param("the-producer-cannot-sign", SigningKeyUnavailableError, id="an-ask-the-producer-cannot-sign-is-retried-and-never-sent"),
    ],
)
@pytest.mark.asyncio
async def test_an_ask_that_cannot_go_out_never_parks_the_promotion(monkeypatch: pytest.MonkeyPatch, event_signer: Any, cause: str, outcome: object) -> None:
    """The return value is the compensating control for a bus that did not take the message.

    If this returned True on a failed publish, the workflow would wait on `promotion_decision` for
    `approval_hours` for an ask that never left the process — the "outage wearing a pause" the
    orchestrator's comment names. Returning False routes it to BLOCKED with "no reachable approver",
    which is a visible outcome.

    An ask the producer cannot sign is not sent at all, since a door would drop it unsigned, and it is not a refusal
    either: the key may resolve within the activity's retry window, so the activity raises and `SIGNING_ACTIVITY_RETRY`
    runs it again.
    """
    sent = _bus(monkeypatch, refuses=cause == "the-bus-refuses-it")
    async with _producer_key(monkeypatch, event_signer(PRODUCER), readable=cause != "the-producer-cannot-sign"):
        asked = _outcome(lambda: wf.request_approval(_ctx(), wf.PromotionSpec.model_validate(_spec())))

    assert asked == outcome
    assert sent == [], f"an ask that could not go out was published: {sent}"
