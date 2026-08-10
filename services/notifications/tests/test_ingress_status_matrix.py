"""The ingress matrix as a MATRIX: one status, one counter emission, and no path that raises.

`test_ingest_handler.py` pins which status each path returns. This suite pins the two things that
travel WITH the status and are invisible to it:

* **The count.** A DROP is an ack — the broker discards the message and the event ceases to exist.
  An uncounted drop therefore leaves no trace anywhere: no row, no redelivery, no series to alert on.
  The same is true of `IGNORED` and `HIDDEN`, which are the two ways this plane deliberately tells
  nobody. So every path is asserted to increment the ingress counter EXACTLY once, with the closed
  `(lane, outcome)` label pair — never zero, never twice.
* **The absence of a raise.** A handler that raises returns a 500 to the sidecar, which is a RETRY it
  did not choose: the same message comes back forever and everything queued behind it waits. The
  matrix below drives every shape a payload can take, including the ones that reach no branch on
  purpose, and the assertion is that a status came back at all.

The `Outcome` a path reports is also a decision rather than a description — `_event_outcome` ranks a
failure above a delivery above a duplicate — so the ranking is driven here with the fan-out shapes v2
will produce, by widening the audience at the one seam that decides it.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

import pytest

from notifications.api import ingest as ingest_module
from notifications.api import metrics as metrics_module
from notifications.api import visibility as visibility_module
from notifications.api.ingest import DAPR_DROP, DAPR_RETRY, DAPR_SUCCESS, ingest_run_event
from notifications.api.lineage_events import Notifiable
from notifications.api.metrics import Lane, Outcome
from notifications.api.visibility import Visibility
from notifications.proxies import TypedActorProxy


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


#: A wired FGA client. Only its identity is used — `_Checks` answers.
WIRED = cast("OpenFgaClient", object())

#: The four authorization configurations an ingress can run under. Strings rather than a bool pair
#: because "off" and "unwired" are different answers to different questions, and a flag would make
#: the third and fourth cases unrepresentable.
FGA_OFF = "off"
FGA_UNWIRED = "unwired"
FGA_ALL = "all"
FGA_NONE = "none"


def _event(
    *,
    event_type: str = "FAIL",
    run_id: str = "run-1",
    author: str | None = "alice",
    outputs: list[str] | None = None,
) -> dict[str, Any]:
    facets: dict[str, Any] = {} if author is None else {"author": {"name": author, "sub": author}}
    return {
        "eventType": event_type,
        "eventTime": "2026-08-09T12:00:00+00:00",
        "run": {"runId": run_id, "facets": facets},
        "outputs": [{"namespace": "silver", "name": name} for name in (outputs if outputs is not None else ["silver$pages"])],
    }


class _Inbox:
    """One subject's inbox with the real actor's idempotency contract: a repeated `notification_id`
    is not delivered twice and says so."""

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
    """A whole actor plane in a dict, addressed the way `inbox_for` addresses the real one."""

    def __init__(self, broken: set[str] | None = None) -> None:
        self.boxes: dict[str, list[dict[str, Any]]] = {}
        self.broken = broken or set()

    def open(self, subject: str) -> TypedActorProxy:
        return cast(TypedActorProxy, _Inbox(self, subject))


class _Checks:
    """Stands in for `service_kit.governed.fga.batch_check`, answering per (subject, object).

    Keyed by subject as well as object because the audience question is asked once per candidate: a
    check that ignored the user would make every recipient's answer the same and hide the whole class
    of bug where one is filtered and another is not.
    """

    def __init__(self, allowed: set[str], *, subjects: set[str] | None = None) -> None:
        self.allowed = allowed
        self.subjects = subjects

    async def __call__(self, client: object, *, user: str, relation: str, objects: list[str], **_: Any) -> dict[str, bool]:
        permitted = self.subjects is None or user in self.subjects
        return {obj: permitted and obj in self.allowed for obj in objects}


class _Counter:
    """Stands in for one OTel counter instrument and records what `record_*` asked it to add.

    The INSTRUMENT rather than the `record_*` function, so the attribute keys the module builds are
    what the assertions see: a counter that fired under the wrong label set is not a counted event,
    it is a second series nothing alerts on.
    """

    def __init__(self) -> None:
        self.adds: list[tuple[int, dict[str, str]]] = []

    def add(self, amount: int, attributes: dict[str, str] | None = None) -> None:
        self.adds.append((amount, dict(attributes or {})))

    @property
    def outcomes(self) -> list[str]:
        return [attributes["lance.notifications.outcome"] for _, attributes in self.adds]


@pytest.fixture
def ingress_counter(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    counter = _Counter()
    monkeypatch.setattr(metrics_module, "_ingress_events", counter)
    return counter


@pytest.fixture
def recipient_counter(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    counter = _Counter()
    monkeypatch.setattr(metrics_module, "_fanout_recipients", counter)
    return counter


def _view(kind: str, monkeypatch: pytest.MonkeyPatch) -> Visibility:
    """One of the four authorization configurations, wired at the `batch_check` seam."""
    if kind == FGA_OFF:
        return Visibility(client=None, enabled=False)
    if kind == FGA_UNWIRED:
        return Visibility(client=None, enabled=True)
    allowed = {"table:silver$pages"} if kind == FGA_ALL else set()
    monkeypatch.setattr(visibility_module.fga, "batch_check", _Checks(allowed))
    return Visibility(client=WIRED, enabled=True)


#: An event whose instant cannot be read — parsed at the boundary, so it never reaches a branch.
_BAD_INSTANT = {"eventType": "FAIL", "eventTime": "nope", "run": {"runId": "r"}}

#: `(payload, fga, broken_subjects, pre_deliveries, status, outcome)` — one row per path the ingress
#: can take. `pre_deliveries` is how many times the same event was already handled, which is the only
#: way to reach DUPLICATE: it is a property of the inbox's history, not of the payload.
_MATRIX = [
    pytest.param(_event(), FGA_OFF, set(), 0, DAPR_SUCCESS, Outcome.DELIVERED, id="a-terminal-run"),
    pytest.param(_event(), FGA_OFF, set(), 1, DAPR_SUCCESS, Outcome.DUPLICATE, id="the-same-run-again"),
    pytest.param(_event(), FGA_ALL, set(), 0, DAPR_SUCCESS, Outcome.DELIVERED, id="a-visible-run-under-fga"),
    pytest.param(_event(), FGA_NONE, set(), 0, DAPR_SUCCESS, Outcome.HIDDEN, id="a-run-the-author-may-not-see"),
    pytest.param(_event(event_type="START"), FGA_OFF, set(), 0, DAPR_SUCCESS, Outcome.IGNORED, id="a-run-that-only-started"),
    pytest.param(_event(event_type="BANANA"), FGA_OFF, set(), 0, DAPR_SUCCESS, Outcome.IGNORED, id="a-state-this-plane-has-never-heard-of"),
    pytest.param(_event(author=None), FGA_OFF, set(), 0, DAPR_SUCCESS, Outcome.IGNORED, id="a-run-with-no-verified-author"),
    pytest.param(_event(author="   "), FGA_OFF, set(), 0, DAPR_SUCCESS, Outcome.IGNORED, id="a-run-whose-author-sub-is-blank"),
    pytest.param(_event(outputs=[]), FGA_OFF, set(), 0, DAPR_SUCCESS, Outcome.IGNORED, id="a-run-that-wrote-no-dataset"),
    pytest.param(None, FGA_OFF, set(), 0, DAPR_DROP, Outcome.DROPPED, id="data-that-is-null"),
    pytest.param([], FGA_OFF, set(), 0, DAPR_DROP, Outcome.DROPPED, id="data-that-is-an-empty-list"),
    pytest.param([_event()], FGA_OFF, set(), 0, DAPR_DROP, Outcome.DROPPED, id="data-that-is-a-list-of-events"),
    pytest.param("a string", FGA_OFF, set(), 0, DAPR_DROP, Outcome.DROPPED, id="data-that-is-a-string"),
    pytest.param(b"{}", FGA_OFF, set(), 0, DAPR_DROP, Outcome.DROPPED, id="data-that-arrived-unparsed"),
    pytest.param({}, FGA_OFF, set(), 0, DAPR_DROP, Outcome.DROPPED, id="data-that-is-an-empty-object"),
    pytest.param(_BAD_INSTANT, FGA_OFF, set(), 0, DAPR_DROP, Outcome.DROPPED, id="an-uninterpretable-instant"),
    pytest.param(_event(), FGA_OFF, {"alice"}, 0, DAPR_RETRY, Outcome.RETRIED, id="an-unreachable-actor-plane"),
    pytest.param(_event(), FGA_UNWIRED, set(), 0, DAPR_RETRY, Outcome.RETRIED, id="authorization-enabled-but-unwired"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("payload", "fga", "broken", "pre_deliveries", "status", "outcome"), _MATRIX)
async def test_every_ingress_path_answers_with_one_of_the_three_sidecar_statuses(
    payload: object,
    fga: str,
    broken: set[str],
    pre_deliveries: int,
    status: dict[str, str],
    outcome: Outcome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity, not equality: the three are module constants because a typo in one of these strings
    is an ack the broker cannot interpret, and `== {"status": "SUCCESS"}` would not catch a fourth
    dict that happens to read the same."""
    plane = _Plane(broken=broken)
    view = _view(fga, monkeypatch)
    for _ in range(pre_deliveries):
        await ingest_run_event(payload, lane=Lane.BUS, visibility=view, open_inbox=plane.open)

    answer = await ingest_run_event(payload, lane=Lane.BUS, visibility=view, open_inbox=plane.open)

    assert answer is status


@pytest.mark.asyncio
@pytest.mark.parametrize(("payload", "fga", "broken", "pre_deliveries", "status", "outcome"), _MATRIX)
async def test_every_ingress_path_counts_itself_exactly_once(
    payload: object,
    fga: str,
    broken: set[str],
    pre_deliveries: int,
    status: dict[str, str],
    outcome: Outcome,
    monkeypatch: pytest.MonkeyPatch,
    ingress_counter: _Counter,
) -> None:
    """A DROP is an ACK: the broker discards the message, so an uncounted drop makes the event cease
    to exist with no trace anywhere. The same holds for the two deliberate silences — `IGNORED` and
    `HIDDEN` — which is why "told nobody" is a counted outcome rather than an early return."""
    plane = _Plane(broken=broken)
    view = _view(fga, monkeypatch)
    for _ in range(pre_deliveries):
        await ingest_run_event(payload, lane=Lane.BUS, visibility=view, open_inbox=plane.open)
    ingress_counter.adds.clear()

    await ingest_run_event(payload, lane=Lane.BUS, visibility=view, open_inbox=plane.open)

    assert ingress_counter.adds == [(1, {"lance.notifications.lane": "bus", "lance.notifications.outcome": outcome.value})]


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", list(Lane))
async def test_the_lane_is_recorded_because_the_two_ingresses_fail_differently(lane: Lane, ingress_counter: _Counter) -> None:
    """The bus and the feed carry DIFFERENT producers — the ingest service and Ray TRAIN reach the
    feed and never the topic — so a lane-blind counter cannot show that one of the two stopped."""
    plane = _Plane()
    await ingest_run_event(_event(), lane=lane, visibility=Visibility(client=None, enabled=False), open_inbox=plane.open)
    assert ingress_counter.adds[0][1]["lance.notifications.lane"] == lane.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        [None],
        "",
        b"",
        0,
        {"data": None},
        {"eventType": None, "eventTime": None, "run": None},
        {"eventType": "FAIL", "eventTime": "2026-08-09T12:00:00+00:00", "run": {"runId": "r", "facets": None}},
        {"eventType": "FAIL", "eventTime": "2026-08-09T12:00:00+00:00", "run": {"runId": "r", "facets": {"author": {"sub": "alice"}}}, "outputs": "not-a-list"},
        {"eventType": "FAIL", "eventTime": "2026-08-09T12:00:00+00:00", "run": {"runId": "r", "facets": {"author": {"sub": "alice"}}}, "outputs": [None]},
    ],
)
async def test_no_payload_shape_makes_the_handler_raise(payload: object) -> None:
    """A raising handler is a RETRY the code did not choose: the sidecar sees a 500, redelivers the
    same message forever, and everything queued behind it waits. Every shape here reaches a branch
    that was written for something else — a facet bag that is null, an output list that is a string —
    and the contract is that a STATUS comes back regardless."""
    plane = _Plane()
    answer = await ingest_run_event(payload, lane=Lane.BUS, visibility=Visibility(client=None, enabled=False), open_inbox=plane.open)
    assert answer in (DAPR_SUCCESS, DAPR_RETRY, DAPR_DROP)


@pytest.mark.asyncio
async def test_a_drop_is_logged_because_nothing_else_will_ever_mention_it(caplog: pytest.LogCaptureFixture) -> None:
    """The counter says one event was dropped; only the log says WHICH producer sent an unparseable
    payload. Both, because a DROP is the one outcome with no redelivery behind it to look at later."""
    plane = _Plane()
    with caplog.at_level(logging.ERROR, logger="notifications.api.ingest"):
        await ingest_run_event("not an event", lane=Lane.FEED, visibility=Visibility(client=None, enabled=False), open_inbox=plane.open)

    dropped = [record for record in caplog.records if record.message == "lineage_event_invalid"]
    assert len(dropped) == 1
    assert dropped[0].levelno == logging.ERROR
    assert getattr(dropped[0], "lane", None) == "feed"


@pytest.mark.asyncio
async def test_a_failed_delivery_names_its_subject_on_the_log_line_and_never_in_a_label(
    caplog: pytest.LogCaptureFixture,
    recipient_counter: _Counter,
) -> None:
    """The subject is per-user data. It belongs where access is already governed — spans and logs —
    and never on a metric label, which would publish the estate's user list into a store nothing
    authorizes reads against while growing one series per person."""
    plane = _Plane(broken={"alice@example.test"})
    with caplog.at_level(logging.ERROR, logger="notifications.api.fanout"):
        await ingest_run_event(
            _event(author="alice@example.test"),
            lane=Lane.BUS,
            visibility=Visibility(client=None, enabled=False),
            open_inbox=plane.open,
        )

    failures = [record for record in caplog.records if record.message == "notification_delivery_failed"]
    assert [(getattr(record, "subject", None), getattr(record, "notification_id", None)) for record in failures] == [("alice@example.test", "run-1@FAIL")]
    assert failures[0].exc_info is not None
    assert recipient_counter.adds == [(1, {"lance.notifications.outcome": "retried"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("fga", [FGA_OFF, FGA_ALL, FGA_NONE, FGA_UNWIRED])
async def test_no_authorization_configuration_puts_a_subject_in_a_metric_label(
    fga: str,
    monkeypatch: pytest.MonkeyPatch,
    ingress_counter: _Counter,
    recipient_counter: _Counter,
) -> None:
    """Swept across every configuration rather than asserted once, because the label set is built at
    four different call sites and a leak needs only one of them."""
    plane = _Plane()
    subject = "alice@example.test"
    await ingest_run_event(_event(author=subject), lane=Lane.BUS, visibility=_view(fga, monkeypatch), open_inbox=plane.open)

    recorded = [attributes for _, attributes in ingress_counter.adds + recipient_counter.adds]
    assert recorded
    for attributes in recorded:
        assert set(attributes) <= {"lance.notifications.lane", "lance.notifications.outcome"}
        assert subject not in attributes.values()


# --- the outcome RANKING: one label for an event whose recipients had different fates -----------
#
# v1's audience is the author alone, so a mixed fan-out is only reachable by widening it at the seam
# that decides it. Driven here rather than deferred to v2 because the ranking is what an operator
# reads, and a rank that only becomes wrong once watches exist is a rank nobody will re-derive then.


def _audience(*subjects: str) -> Callable[..., Awaitable[tuple[str, ...]]]:
    """A stand-in for `audience_for` — the author plus the project's watchers, resolved.

    Async and `**_` tolerant since S4: the real one awaits a watcher lookup, and a stand-in that took
    only the notice would pass here while the production call site (`watchers=`) raised.
    """

    async def resolve(notice: Notifiable, **_: object) -> tuple[str, ...]:
        return subjects

    return resolve


@pytest.mark.asyncio
async def test_a_partly_failed_fan_out_is_counted_as_retried_rather_than_delivered(
    monkeypatch: pytest.MonkeyPatch,
    ingress_counter: _Counter,
) -> None:
    """A failure outranks a delivery. Labelling this event DELIVERED because somebody got it would
    make a partial outage invisible in the one series that could show it — and the event IS being
    redelivered, so counting it as done would double-count it on the next attempt."""
    plane = _Plane(broken={"bob"})
    monkeypatch.setattr(ingest_module, "audience_for", _audience("alice", "bob"))

    answer = await ingest_run_event(_event(), lane=Lane.BUS, visibility=Visibility(client=None, enabled=False), open_inbox=plane.open)

    assert answer is DAPR_RETRY
    assert ingress_counter.outcomes == [Outcome.RETRIED.value]


@pytest.mark.asyncio
async def test_an_event_one_recipient_may_not_see_is_still_a_delivered_event(
    monkeypatch: pytest.MonkeyPatch,
    ingress_counter: _Counter,
) -> None:
    """`HIDDEN` is reported only when it is the WHOLE story. An event that reached somebody is a
    delivered event; calling it half-hidden would put ordinary per-tenant filtering into the series
    an operator watches for "nobody is being told anything"."""
    plane = _Plane()
    monkeypatch.setattr(ingest_module, "audience_for", _audience("alice", "bob"))
    monkeypatch.setattr(visibility_module.fga, "batch_check", _Checks({"table:silver$pages"}, subjects={"alice"}))
    seen_by_alice_only = Visibility(client=WIRED, enabled=True)

    answer = await ingest_run_event(_event(), lane=Lane.BUS, visibility=seen_by_alice_only, open_inbox=plane.open)

    assert answer is DAPR_SUCCESS
    assert ingress_counter.outcomes == [Outcome.DELIVERED.value]
    assert list(plane.boxes) == ["alice"]


@pytest.mark.asyncio
async def test_an_audience_that_already_has_the_pointer_is_a_duplicate_not_a_delivery(
    monkeypatch: pytest.MonkeyPatch,
    ingress_counter: _Counter,
) -> None:
    """DUPLICATE is counted rather than silent: it is the evidence the natural key is doing its job,
    and the series that would show a redelivery storm."""
    plane = _Plane()
    monkeypatch.setattr(ingest_module, "audience_for", _audience("alice", "bob"))
    view = Visibility(client=None, enabled=False)
    await ingest_run_event(_event(), lane=Lane.BUS, visibility=view, open_inbox=plane.open)
    ingress_counter.adds.clear()

    await ingest_run_event(_event(), lane=Lane.BUS, visibility=view, open_inbox=plane.open)

    assert ingress_counter.outcomes == [Outcome.DUPLICATE.value]


@pytest.mark.asyncio
async def test_an_empty_audience_is_acked_rather_than_retried_forever(
    monkeypatch: pytest.MonkeyPatch,
    ingress_counter: _Counter,
) -> None:
    """Nobody to tell is a handled event. RETRY here would redeliver an event that can never change
    its answer until the sidecar dead-letters it — a DLQ filled with successfully-handled messages."""
    plane = _Plane()
    monkeypatch.setattr(ingest_module, "audience_for", _audience())

    answer = await ingest_run_event(_event(), lane=Lane.BUS, visibility=Visibility(client=None, enabled=False), open_inbox=plane.open)

    assert answer is DAPR_SUCCESS
    assert ingress_counter.outcomes == [Outcome.HIDDEN.value]
    assert plane.boxes == {}
