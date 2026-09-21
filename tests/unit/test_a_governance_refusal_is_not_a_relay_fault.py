"""`refused` is its own number, because `stranded` already means something else.

[[LH-004]] MEASURED ON THE LIVE ESTATE, repeatedly and unchanged: `lineage_outbox_drained drained=0
stranded=6`, with 24 `lineage_outbox_event_unauthorized` lines in twenty minutes across 6 distinct
`run_id`s, all `author='e2e'`, against `e2e_crash_ds` and `e2e_outbox_ds`. Those six have been staged
for 2.1 days (`outbox_oldest_age_seconds` = 179206 on the `rask-lineage` lane) and will strand forever:
the graph's answer is deterministic, so every tick re-refuses them.

`DrainOutcome`'s own docstring says the pair exists so that "drained alone says a tick succeeded while a
specific event has been refused on every tick since the estate came up; stranded alone says a tick
FAILED while it recovered everything else". A governance refusal is not a failed tick — it is a settled
answer about a well-formed event — so folding it into `stranded` makes the number mean both things at
once, and `drained=0 stranded=6` then reads as a wedged relay when the relay is healthy (it drained
`drained=1 stranded=0` the moment ingest's credential landed).

WHY NOT RETIRE THE EVENT INSTEAD, which is the obvious remedy and is NOT available: moving it to a
`<outbox>/_refused/` prefix is a PutObject, and the relay is denied that by policy —
`test_the_lineage_plane_writes_nothing_it_does_not_own.py` asserts
`not (allowed & {"s3:PutObject", ...})`, with DeleteObject scoped to `/_lineage_outbox/*`. Widening that
grant to tidy a counter would trade a real least-privilege boundary for a cosmetic one. So the event
stays staged and recoverable if the grant ever lands; what changes is which number counts it.

THE EVENT IS STILL STRANDED IN THE LITERAL SENSE — left staged, not dropped — and that is deliberate.
This splits the REPORTING, not the handling.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError

from lineage.api import reconcile_cron
from medallion.schemas.events import build_run_event
from service_kit.lakehouse import outbox


def _staged(uri: str, *, author: str, run: str, token: str) -> None:
    """Stage one event. THE TOKEN MUST DIFFER PER EVENT, and that is the idempotency key working rather
    than a quirk: `run_id` is derived from it, so two events sharing a token are ONE run and the second
    `stage_event` overwrites the first. Measured while writing this file — a two-event fixture staged
    one object, and the isolation leg below silently tested nothing."""
    event = build_run_event(
        operation="ingest_events",
        author=author,
        job_namespace="medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="bronze",
        output_name=run,
        version=2,
        token=token,
    )
    outbox.stage_event(uri, {}, event["run"]["runId"], json.dumps(event))


class _Repo:
    def __init__(self) -> None:
        self.ingested: list[str] = []
        self.refusals: list[dict[str, str | None]] = []

    async def record_refusal(self, *, outbox_key: str, run_id: str, author: str | None, reason: str, event_json: str) -> None:
        """[[LH-182]] The drain now RECORDS a settled refusal before retiring the object, so a double
        that cannot record one no longer stands in for the repository. Captured rather than ignored:
        several of these tests assert what the refusal path did, and a silent no-op would let a drain
        that recorded nothing pass as one that did."""
        self.refusals.append({"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event_json": event_json})

    async def ingest_event(self, ev: Any) -> None:  # noqa: ANN401 — the drain's own shape
        self.ingested.append(ev.run.run_id)


def _settings(uri: str) -> Any:  # noqa: ANN401 — a stand-in for the drain's settings protocol
    class _S:
        outbox_uri = uri
        outbox_drain_limit = 500
        dapr_pubsub = "lineage-pubsub"
        dapr_topic = "lineage.events.v1"
        dapr_publish_timeout_seconds = 5.0
        fga_enabled = True

    return _S()


def _drive(uri: str) -> Any:  # noqa: ANN401 — DrainOutcome
    request = cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    return asyncio.run(reconcile_cron._drain_outbox(request, cast("Any", _Repo()), _settings(uri), {}))


def _drive_capturing(uri: str) -> tuple[_Repo, Any]:
    """Drive a drain and hand back the REPOSITORY and the outcome, so a test can assert both.

    Separate from `_drive` rather than changing its return: every other test here reads only the
    counters, and widening the shared helper would edit them all to assert nothing new.
    """
    request = cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    repo = _Repo()
    outcome = asyncio.run(reconcile_cron._drain_outbox(request, cast("Any", repo), _settings(uri), {}))
    return repo, outcome


def test_a_refused_event_counts_as_REFUSED_not_stranded(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE SPLIT. `stranded` is documented as "a tick failed while it recovered everything else"; a
    governance refusal is neither a failure nor transient, and counting it there is what makes
    `drained=0 stranded=6` read as a wedged relay on a healthy one."""

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionDeniedError("can_write_data required to amend run")

    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refuse)
    uri = f"file://{tmp_path}/_lineage_outbox"
    _staged(uri, author="e2e", run="bronze$refused", token="refused-probe")

    outcome = _drive(uri)

    assert outcome.refused == 1, "a governance refusal has no number of its own, so it is still reported as a relay fault"
    assert outcome.stranded == 0, "`stranded` must mean what it is documented to mean: a tick that FAILED"
    assert outcome.drained == 0


def test_the_refused_event_is_RECORDED_BEFORE_IT_IS_RETIRED(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The property is unchanged; the mechanism that keeps it is not ([[LH-182]]).

    THE RULE WAS "never destroy the only durable copy of a committed write's provenance", and leaving
    the object staged was how that was honoured — at the cost of re-reading, re-parsing, re-refusing
    and re-logging the same event on every tick forever (measured `refused=7`, unchanged for days).
    There is now a second durable copy: the drain writes the verdict AND the event into
    `lineage_outbox_refusals` before removing the object, so retiring it destroys nothing.

    WHAT WOULD STILL BE WRONG is deleting without that record, and the ordering assertion in
    `services/lineage/tests/test_a_permanent_refusal_reaches_a_terminal_state.py` is what forbids it.
    This test asserts the pair: the refusal was captured, and only then did the object go.
    """

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionDeniedError("can_write_data required to amend run")

    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refuse)
    uri = f"file://{tmp_path}/_lineage_outbox"
    _staged(uri, author="e2e", run="bronze$refused", token="refused-probe")

    repo, first = _drive_capturing(uri)

    assert repo.refusals, "the refusal was not recorded, so retiring the object would be data loss"
    assert repo.refusals[0]["event_json"], "the record kept no event; a refusal with no payload is loss, not a loss RECORD"
    assert not list(outbox.list_events(uri, {})), "the object survived a settled refusal, so it will be re-refused every tick forever"

    # AND THE TICK SAYS SO ([[LH-182]]). `refused` counts refusals HANDLED and reads identically whether
    # the object was retired or re-refused for the hundredth time — measured on the deployed estate,
    # `refused=7` before the drain existed and `refused=7` after it, with no way to tell them apart.
    assert first.recorded == first.refused == 1, f"the tick reported recorded={first.recorded} against refused={first.refused}"

    # THE SECOND TICK IS THE PROOF THE FIRST ONE CANNOT GIVE: a re-refusing loop would report the same
    # numbers again forever, and a closed loop finds nothing left to refuse.
    _, second = _drive_capturing(uri)
    assert second.refused == second.recorded == 0, f"a second tick still saw refused={second.refused}; the object was not retired"


def test_a_TRANSIENT_failure_still_strands(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the split, and the one that keeps `stranded` meaningful. A store outage or an
    expired credential IS a failed tick, retried next time, and must not be laundered into the settled
    class — otherwise the new number absorbs the old one and nothing is distinguishable again."""

    async def _blow_up(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("the graph did not answer")

    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _blow_up)
    uri = f"file://{tmp_path}/_lineage_outbox"
    _staged(uri, author="e2e", run="bronze$transient", token="transient-probe")

    outcome = _drive(uri)

    assert outcome.stranded == 1, "a transient failure is exactly what `stranded` is for"
    assert outcome.refused == 0, "a transient failure is not a governance answer"


def test_one_refusal_does_not_decide_anything_about_the_others(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-event isolation, re-asserted across the new boundary. The drain already learned this the hard
    way — one un-ingestable event stranded every other staged event permanently — and a new counter must
    not reintroduce it by aborting the loop."""
    seen: list[str] = []

    async def _refuse_one(event: Any, *_args: object, **_kwargs: object) -> None:  # noqa: ANN401
        names = [output.name for output in (getattr(event, "outputs", None) or [])]
        seen.extend(names)
        if any("refused" in name for name in names):
            raise PermissionDeniedError("can_write_data required to amend run")

    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refuse_one)
    uri = f"file://{tmp_path}/_lineage_outbox"
    _staged(uri, author="e2e", run="bronze$refused", token="refused-probe")
    _staged(uri, author="alice", run="bronze$fine", token="fine-probe")

    outcome = _drive(uri)

    assert outcome.refused == 1, "the refusal was not counted"
    assert len(seen) == 2, "the drain stopped at the refusal instead of continuing — the defect per-event isolation exists to prevent"
