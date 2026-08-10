"""Who gets told — the audience rules, at the seam where the ungoverned bus meets a person's inbox.

`test_visibility.py` pins the SHAPE of the question `Visibility` asks. This suite pins what the
fan-out does with the answers, which is where the rules either hold or quietly stop holding:

* one `batch_check` per candidate recipient, covering every output at once — never one round-trip per
  object, and never one answer reused across subjects;
* the subset rule (`names <= visible`) applied per recipient, so one invisible output removes that
  recipient and nobody else;
* a dataset-less run refused BEFORE authorization is consulted at all, because the visibility test
  answers an empty candidate set permissively — by design, and for a different question;
* fail-closed on an FGA outage, on this lane too: RETRY, never DROP, never a delivery;
* one bad recipient never aborting the audience, with the failure recorded rather than swallowed.
"""

import logging
from typing import TYPE_CHECKING, Any, cast

import pytest

from notifications.api import metrics as metrics_module
from notifications.api import visibility as visibility_module
from notifications.api.fanout import audience_for, fan_out
from notifications.api.ingest import DAPR_DROP, DAPR_RETRY, DAPR_SUCCESS, ingest_run_event
from notifications.api.lineage_events import LineageRunEvent, Notifiable, notifiable
from notifications.api.metrics import Lane
from notifications.api.visibility import FGA_OBJECT_TYPE, METADATA_RELATION, NOTIFY_RELATION, Visibility
from notifications.proxies import TypedActorProxy, inbox_actor_id
from service_kit.exceptions import ServiceUnavailableError


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


WIRED = cast("OpenFgaClient", object())
OPEN = Visibility(client=None, enabled=False)


def _event(*, author: str = "alice", outputs: list[str] | None = None) -> dict[str, Any]:
    return {
        "eventType": "FAIL",
        "eventTime": "2026-08-09T12:00:00+00:00",
        "run": {"runId": "run-1", "facets": {"author": {"name": author, "sub": author}}},
        "outputs": [{"namespace": "silver", "name": name} for name in (outputs if outputs is not None else ["silver$pages"])],
    }


def _notice(*, author: str = "alice", outputs: list[str] | None = None) -> Notifiable:
    notice = notifiable(LineageRunEvent.model_validate(_event(author=author, outputs=outputs)))
    assert notice is not None
    return notice


class _Inbox:
    def __init__(self, plane: "_Plane", subject: str) -> None:
        self._plane = plane
        self._subject = subject

    async def deliver(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._subject in self._plane.broken:
            raise RuntimeError("the sidecar is unreachable")
        rows = self._plane.boxes.setdefault(self._subject, [])
        if any(row["notification_id"] == payload["notification_id"] for row in rows):
            return {"delivered": False, "unread": len(rows), "rows": len(rows)}
        rows.append(payload)
        return {"delivered": True, "unread": len(rows), "rows": len(rows)}


class _Plane:
    def __init__(self, broken: set[str] | None = None) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}
        self.broken = broken or set()

    def open(self, subject: str) -> TypedActorProxy:
        return cast(TypedActorProxy, _Inbox(self, subject))


class _Recorder:
    """Stands in for `fga.batch_check` and records every round-trip it was asked to make."""

    def __init__(self, allowed: dict[str, set[str]]) -> None:
        self.allowed = allowed
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, client: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        self.calls.append({"user": user, "relation": relation, "objects": list(objects)})
        permitted = self.allowed.get(user, set())
        return {obj: obj in permitted for obj in objects}


class _Counter:
    """Stands in for one OTel counter instrument — see `test_ingress_status_matrix.py`."""

    def __init__(self) -> None:
        self.adds: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        self.adds.append((amount, dict(attributes or {})))

    @property
    def outcomes(self) -> list[str]:
        return [attributes["lance.notifications.outcome"] for _, attributes in self.adds]


@pytest.fixture
def recipient_counter(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    counter = _Counter()
    monkeypatch.setattr(metrics_module, "_fanout_recipients", counter)
    return counter


def _governed(monkeypatch: pytest.MonkeyPatch, allowed: dict[str, set[str]]) -> tuple[Visibility, _Recorder]:
    recorder = _Recorder(allowed)
    monkeypatch.setattr(visibility_module.fga, "batch_check", recorder)
    return Visibility(client=WIRED, enabled=True), recorder


# --- v1's audience -------------------------------------------------------------------------------


def test_the_audience_is_the_verified_author_and_nobody_else() -> None:
    """Membership gates watching and never implies it. The failure this plane exists to end is a badge
    that counts other people's work, and it returns the moment an audience widens by default."""
    assert audience_for(_notice(author="alice")) == ("alice",)


def test_the_audience_follows_the_run_rather_than_any_configuration() -> None:
    assert audience_for(_notice(author="bob")) == ("bob",)


# --- the round-trip shape ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_round_trip_covers_every_output_of_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """`batch_check` over the whole output set, never one `check` per object: the delivery decision is
    a single subset test, and asking it N times turns one authorization round-trip per event into one
    per dataset on a lane that sees every tenant's runs."""
    view, recorder = _governed(monkeypatch, {"alice": {f"{FGA_OBJECT_TYPE}:{n}" for n in ("a", "b", "c")}})
    plane = _Plane()

    await fan_out(_notice(outputs=["a", "b", "c"]), audience=["alice"], visibility=view, open_inbox=plane.open)

    assert len(recorder.calls) == 1
    assert sorted(recorder.calls[0]["objects"]) == ["table:a", "table:b", "table:c"]


@pytest.mark.asyncio
async def test_each_candidate_is_asked_separately_because_the_answer_is_per_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """One batch per recipient, not one batch reused: `can_get_metadata` is a question about a
    (subject, object) pair, and caching the first answer across an audience would deliver one person's
    grants to everyone in it."""
    view, recorder = _governed(monkeypatch, {"alice": {"table:silver$pages"}, "bob": set()})
    plane = _Plane()

    result = await fan_out(_notice(), audience=["alice", "bob"], visibility=view, open_inbox=plane.open)

    assert [call["user"] for call in recorder.calls] == ["alice", "bob"]
    assert (result.delivered, result.hidden) == (1, 1)
    assert list(plane.boxes) == ["alice"]


@pytest.mark.asyncio
async def test_the_objects_are_addressed_as_tables_and_never_as_a_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """The FGA position, pinned where it is USED rather than only where it is written.

    Two claims, and the second is S4's. (1) The objects are addressed as `table:` — a delivery check
    that named a CONTAINER would reach every subject holding `reader` on any one table beneath it,
    because `can_get_metadata` on `warehouse`/`namespace` is `reader or can_get_metadata from child`
    since C1. (2) The relation is `can_be_notified`, not the render's `can_get_metadata`: a
    notification asserts a stake in the object itself. On `table` the two are the same set, so this
    changed no audience — which is exactly why the split had to be pinned rather than trusted to be
    noticed later.
    """
    view, recorder = _governed(monkeypatch, {"alice": set()})
    plane = _Plane()

    await fan_out(_notice(outputs=["silver$pages", "gold$lines"]), audience=["alice"], visibility=view, open_inbox=plane.open)

    assert recorder.calls[0]["relation"] == NOTIFY_RELATION
    assert recorder.calls[0]["relation"] != METADATA_RELATION, "delivery must not ask the render's question"
    for obj in recorder.calls[0]["objects"]:
        assert obj.startswith("table:")


@pytest.mark.asyncio
async def test_the_render_path_keeps_the_permissive_relation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the split — and the half that must NOT tighten.

    A pointer already in an inbox resolves through `visible()`. Asking the delivery question there
    would blank rows the delivery gate had already approved, on a container-scoped pointer that the
    subject can legitimately see. Delivery decides who is TOLD; render decides what still resolves.
    """
    view, recorder = _governed(monkeypatch, {"alice": set()})

    await view.visible("alice", ["silver$pages"])

    assert recorder.calls[0]["relation"] == METADATA_RELATION


# --- the subset rule -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_invisible_output_removes_that_recipient_and_nobody_else(monkeypatch: pytest.MonkeyPatch, recipient_counter: _Counter) -> None:
    """`names <= visible` per recipient. A run that wrote a dataset you may read and one you may not
    is a run you are not told about — being told names the run, its author and its outcome — but that
    is a decision about YOU, so it must not remove anyone who can see both."""
    view, _ = _governed(
        monkeypatch,
        {"alice": {"table:silver$pages"}, "bob": {"table:silver$pages", "table:gold$lines"}},
    )
    plane = _Plane()

    result = await fan_out(_notice(outputs=["silver$pages", "gold$lines"]), audience=["alice", "bob"], visibility=view, open_inbox=plane.open)

    assert (result.delivered, result.hidden, result.failed) == (1, 1, 0)
    assert list(plane.boxes) == ["bob"]
    assert recipient_counter.outcomes == ["hidden", "delivered"]


@pytest.mark.asyncio
async def test_a_hidden_recipient_is_never_a_reason_to_redeliver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hidden is permanent for this event. RETRY would re-offer it until the sidecar exhausts its
    schedule and parks it — a dead-letter queue filling up with correctly-handled messages."""
    view, _ = _governed(monkeypatch, {"alice": set()})
    plane = _Plane()

    answer = await ingest_run_event(_event(), lane=Lane.BUS, visibility=view, open_inbox=plane.open)

    assert answer is DAPR_SUCCESS
    assert plane.boxes == {}


# --- the dataset-less run: refused one step EARLIER than authorization ---------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False], ids=["authorization-on", "authorization-off"])
async def test_a_dataset_less_run_is_refused_under_either_authorization_setting(enabled: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """One rule everywhere, and no configuration in which it relaxes.

    A pointer names the object it is about; a run that wrote nothing has no honest value for that
    field. Parametrized over both settings rather than asserted once because "refused when FGA is on"
    is the half that comes for free — the governed read path drops these anyway — and the half worth
    gating is that turning authorization OFF does not turn the rule off with it.
    """
    view = Visibility(client=WIRED, enabled=True) if enabled else OPEN
    if enabled:
        monkeypatch.setattr(visibility_module.fga, "batch_check", _Recorder({}))
    plane = _Plane()

    answer = await ingest_run_event(_event(outputs=[]), lane=Lane.BUS, visibility=view, open_inbox=plane.open)

    assert answer is DAPR_SUCCESS
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_the_visibility_check_alone_would_pass_a_dataset_less_run_for_every_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old form, built and shown broken — and the reason the projection refuses it first.

    `Visibility.visible` short-circuits an EMPTY candidate set before it consults OpenFGA, which is
    correct for its own question (nothing can be disclosed by answering an empty one, and 503ing a
    page that had no rows to filter would turn "your inbox is empty" into an error). But it makes
    `sees_all(subject, [])` vacuously true for every subject alive — so a plane that gated only on
    visibility would push a run's id, author and outcome to a caller holding no grants at all.
    """
    view, recorder = _governed(monkeypatch, {})

    assert await view.sees_all("a-subject-with-no-grants-at-all", [])
    assert recorder.calls == [], "and it never even asked, so there is no refusal to notice"


# --- fail-closed ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorization_enabled_but_unwired_is_a_retry_and_never_a_drop() -> None:
    """The middle case of the three-outcome rule. DROP would ack an event nobody was told about — the
    outage would be indistinguishable from a run nobody needed to hear about — so the answer has to be
    the one that neither delivers nor discards while the authorization layer is broken."""
    plane = _Plane()

    answer = await ingest_run_event(_event(), lane=Lane.BUS, visibility=Visibility(client=None, enabled=True), open_inbox=plane.open)

    assert answer is DAPR_RETRY
    assert answer is not DAPR_DROP
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_an_authorization_outage_is_never_answered_permissively_for_any_recipient() -> None:
    """Swept over an audience rather than one subject: the refusal is raised per recipient, so a
    fan-out is where a "well, carry on for the rest" would be written."""
    plane = _Plane()
    unwired = Visibility(client=None, enabled=True)

    result = await fan_out(_notice(), audience=["alice", "bob", "carol"], visibility=unwired, open_inbox=plane.open)

    assert (result.delivered, result.failed) == (0, 3)
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_the_unwired_refusal_is_the_estates_own_service_unavailable() -> None:
    """Raised as `ServiceUnavailableError` rather than a bare exception so the HTTP door renders it as
    503 problem+json through the shared handlers, instead of a 500 that reads as "notifications are
    broken" when the broken thing is authorization."""
    with pytest.raises(ServiceUnavailableError):
        await Visibility(client=None, enabled=True).sees_all("alice", ["silver$pages"])


# --- partial failure -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_recipient_never_aborts_the_audience_and_the_failure_is_recorded(
    recipient_counter: _Counter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fan-out that stopped at the first failure would deliver to some subset of the people who
    should have had it, and the subset would depend on iteration order — the same event losing a
    different recipient each time it is redelivered. Recorded as well as survived: a swallowed
    per-recipient failure is a person who is never told and nothing that says so."""
    plane = _Plane(broken={"bob"})

    with caplog.at_level(logging.ERROR, logger="notifications.api.fanout"):
        result = await fan_out(_notice(), audience=["bob", "alice", "carol"], visibility=OPEN, open_inbox=plane.open)

    assert (result.delivered, result.failed) == (2, 1)
    assert sorted(plane.boxes) == ["alice", "carol"]
    assert recipient_counter.outcomes == ["retried", "delivered", "delivered"]
    assert [getattr(record, "subject", None) for record in caplog.records if record.message == "notification_delivery_failed"] == ["bob"]


@pytest.mark.asyncio
async def test_the_retry_that_follows_a_partial_failure_tells_nobody_twice() -> None:
    """RETRY re-drives the WHOLE audience, which is only safe because delivery is idempotent on the
    natural key. Without that, "one bad recipient" would be a choice between losing deliveries and
    doubling them, and doubling is the one a queue makes by default."""
    plane = _Plane(broken={"bob"})
    notice = _notice()

    first = await fan_out(notice, audience=["bob", "alice"], visibility=OPEN, open_inbox=plane.open)
    assert first.needs_retry

    plane.broken.clear()
    second = await fan_out(notice, audience=["bob", "alice"], visibility=OPEN, open_inbox=plane.open)

    assert (second.delivered, second.duplicate, second.failed) == (1, 1, 0)
    assert [len(rows) for rows in plane.boxes.values()] == [1, 1]


# --- the blank subject: a permanent fault must not be classified as transient --------------------


@pytest.mark.asyncio
async def test_a_run_whose_author_sub_is_blank_is_ignored_rather_than_retried_forever() -> None:
    """There is no anonymous inbox, so a blank subject is a permanent fault — and the projection
    refuses it as "no verified author" before anything tries to address an actor with it."""
    plane = _Plane()

    answer = await ingest_run_event(_event(author="   "), lane=Lane.BUS, visibility=OPEN, open_inbox=plane.open)

    assert answer is DAPR_SUCCESS
    assert plane.boxes == {}


@pytest.mark.asyncio
async def test_letting_a_blank_subject_reach_the_inbox_would_redeliver_a_permanent_fault() -> None:
    """The old form, and what it would cost. `inbox_actor_id` refuses a blank subject — correctly:
    there is no anonymous inbox. But the fan-out's per-recipient catch is deliberately broad, so that
    refusal arrives as a RETRIED recipient, which is a RETRY, which is a payload the sidecar will
    re-offer until it dead-letters it. Classifying it needs the projection's guard, not the inbox's.
    """
    with pytest.raises(ValueError, match="non-empty verified subject"):
        inbox_actor_id("   ")

    plane = _Plane()
    result = await fan_out(_notice(), audience=["   "], visibility=OPEN, open_inbox=lambda subject: plane.open(inbox_actor_id(subject)))

    assert result.needs_retry, "a permanent fault, wearing a transient answer"
