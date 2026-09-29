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
from notifications.api.lineage_events import LineageRunEvent, Notifiable, notifiable
from notifications.api.visibility import METADATA_RELATION, NOTIFY_RELATION, Visibility
from notifications.proxies import TypedActorProxy


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


WIRED = cast("OpenFgaClient", object())
OPEN = Visibility(client=None, enabled=False)


def _event(*, author: str = "alice", outputs: list[str] | None = None, project: str | None = None) -> dict[str, Any]:
    facets: dict[str, Any] = {"author": {"name": author, "sub": author}}
    if project is not None:
        # The same `lance` facet `run_id` rides — the producer stamps the tenant there.
        facets["lance"] = {"project": project}
    return {
        "eventType": "FAIL",
        "eventTime": "2026-08-09T12:00:00+00:00",
        "run": {"runId": "run-1", "facets": facets},
        "outputs": [{"namespace": "silver", "name": name} for name in (outputs if outputs is not None else ["silver$pages"])],
    }


def _notice(*, author: str = "alice", outputs: list[str] | None = None, project: str | None = None) -> Notifiable:
    notice = notifiable(LineageRunEvent.model_validate(_event(author=author, outputs=outputs, project=project)))
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


@pytest.mark.asyncio
async def test_a_project_watch_widens_the_audience_but_never_replaces_the_author() -> None:
    """v2 targeting: author UNION watchers, author FIRST — the one recipient guaranteed to exist."""

    async def watchers(project: str) -> list[str]:
        assert project == "acme"
        return ["bob", "carol"]

    audience = await audience_for(_notice(author="alice", project="acme"), watchers=watchers)

    assert audience == ("alice", "bob", "carol")


@pytest.mark.asyncio
async def test_an_author_who_also_watches_is_told_once() -> None:
    """Deduped, so a project owner who watches their own project does not get two pointers."""

    async def watchers(_project: str) -> list[str]:
        return ["alice", "bob"]

    assert await audience_for(_notice(author="alice", project="acme"), watchers=watchers) == ("alice", "bob")


@pytest.mark.asyncio
async def test_a_run_with_no_project_reaches_its_author_and_no_watchers() -> None:
    """A producer that stamps no `lance.project` facet is not an error — it is v1's case, still.

    Silence for the watchers is the safe direction: the alternative is guessing a tenant and
    notifying the wrong one.
    """

    async def watchers(_project: str) -> list[str]:
        raise AssertionError("a project-less run must not resolve watchers at all")

    assert await audience_for(_notice(author="alice"), watchers=watchers) == ("alice",)


# --- the round-trip shape ------------------------------------------------------------------------


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


# --- fail-closed ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_authorization_outage_is_never_answered_permissively_for_any_recipient() -> None:
    """Swept over an audience rather than one subject: the refusal is raised per recipient, so a
    fan-out is where a "well, carry on for the rest" would be written."""
    plane = _Plane()
    unwired = Visibility(client=None, enabled=True)

    result = await fan_out(_notice(), audience=["alice", "bob", "carol"], visibility=unwired, open_inbox=plane.open)

    assert (result.delivered, result.failed) == (0, 3)
    assert plane.boxes == {}


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


# --------------------------------------------------------------------------------------------------
# v5 — the ORIGINATOR: the human whose work a service-authored run is doing
# --------------------------------------------------------------------------------------------------


def _cascade_event(*, originator: str | None, author: str = "data_eng") -> dict[str, Any]:
    """A stage runner's FAIL, shaped as the medallion really emits one.

    `author` is a ROLE LITERAL from chart values (`data_eng`/`analyst`/`htr`/`ray`), which is the whole
    problem: it is a truthful statement about who ran the stage and a useless inbox address. The human
    who started the cascade rides `lance.originator`.
    """
    lance: dict[str, Any] = {"operation": "embed_features", "project": "acme"}
    if originator is not None:
        lance["originator"] = originator
    return {
        "eventType": "FAIL",
        "eventTime": "2026-08-16T12:00:00+00:00",
        "run": {"runId": "run-cascade-1", "facets": {"author": {"name": author, "sub": author}, "lance": lance}},
        "outputs": [{"namespace": "silver", "name": "acme-silver$features"}],
    }


@pytest.mark.asyncio
async def test_an_originator_is_told_for_that_reason_and_not_as_the_author() -> None:
    """The reason is stored, never inferred, because a delivery re-check keys on it: "you ran this" and
    "this ran on your behalf" are different claims, and a row that could not tell them apart could not
    be re-checked against the right rule later."""
    plane = _Plane()
    notice = notifiable(LineageRunEvent.model_validate(_cascade_event(originator="alice")))
    assert notice is not None
    await fan_out(notice, audience=await audience_for(notice), visibility=OPEN, open_inbox=plane.open)
    assert plane.boxes["alice"][0]["reason"] == "originator"
    assert plane.boxes["data_eng"][0]["reason"] == "author"


@pytest.mark.asyncio
async def test_an_originator_who_is_also_the_author_is_told_once() -> None:
    """Same dedupe rule the watcher lane already has: the audience is ordered and deduped, so a human
    who triggered a run that names them as author too gets one row, not two."""
    notice = notifiable(LineageRunEvent.model_validate(_cascade_event(originator="alice", author="alice")))
    assert notice is not None
    assert await audience_for(notice) == ("alice",)
