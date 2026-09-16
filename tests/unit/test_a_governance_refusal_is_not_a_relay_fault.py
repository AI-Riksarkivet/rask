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


def test_the_refused_event_is_still_LEFT_STAGED(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """This changes the REPORTING, not the handling. Destroying the only durable copy of a committed
    write's provenance is still the wrong answer to "you may not record this", and the grant may yet
    land — ingest's did, and its event drained on the next tick."""

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionDeniedError("can_write_data required to amend run")

    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refuse)
    uri = f"file://{tmp_path}/_lineage_outbox"
    _staged(uri, author="e2e", run="bronze$refused", token="refused-probe")

    _drive(uri)

    assert list(outbox.list_events(uri, {})), "the refusal deleted the staged object — that is the poison path, and a refusal is not poison"


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
