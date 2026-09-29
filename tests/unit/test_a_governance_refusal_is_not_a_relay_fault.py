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

WHY NOT MOVE THE EVENT ASIDE to a `<outbox>/_refused/` prefix: that is a PutObject, and the chart grants
the relay exactly `s3:DeleteObject` on its own outbox (`DrainItsOwnOutboxAndNothingElse` in
`chart/templates/minio-scoped-users.yaml`). Widening that grant to tidy a counter would trade a real
least-privilege boundary for a cosmetic one. The durable copy of a refusal is a row instead: the verdict
and the event go to `lineage_outbox_refusals` first ([[LH-182]]), and only then is the object dropped.
Never destroy the only durable copy of a committed write's provenance to answer "you may not record this".

STAGING AN EVENT IS NOT A WAY AROUND THE GATE PUBLISHING ONE HAS TO CLEAR. Four paths reach
`repository.ingest_event`: the HTTP door (`enforce_author` then `enforce_output_authz`), the bus
(`authorize(event)`, DROP on denial), the DLQ replay door (`enforce_output_authz`), and this outbox
relay. The outbox is writable by the producers that stage there through the vended outbox credential
(ingest, maintenance, medallion), so a relay that ingested unchecked would let a stager that cannot get
an event past the bus door stage it instead. The relay calls the SAME `enforce_bus_authz`, which
authorizes AS the subject the producer stamped, the only principal a cron tick has; a second copy of
"may you record this" is how the two doors would drift.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError

from lineage.api import reconcile_cron
from lineage.core.config import LineageSettings
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

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
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


def _governed_settings(outbox_uri: str) -> LineageSettings:
    """The real settings, FGA on — the whole point: the other executing tests leave it off."""
    auth = {"oidc_enabled": True, "oidc_issuer": "https://dex.example", "oidc_audience": "lance", "fga_store_id": "s", "fga_model_id": "m"}
    return LineageSettings.model_validate({"database_url": "postgresql://x/y", "outbox_uri": outbox_uri, "fga_enabled": True, **auth})


def test_a_refused_event_is_counted_apart_and_dropped_only_after_its_record(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The drain's outcomes mean different things, and a refusal is its own.

    Poison is DROPPED because a malformed object wedges the drain forever. A refusal is well-formed and
    deterministic, so it lands on `refused`, never `stranded`, and its object is retired only AFTER the
    verdict and the event are recorded ([[LH-182]]): a drop first would destroy the only durable copy of
    a committed write's provenance. DRIVEN, with the order observed at the two calls themselves.
    """

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionDeniedError("can_write_data required")

    steps: list[str] = []
    real_drop = outbox.drop_event

    def _drop(*args: Any) -> None:  # noqa: ANN401 — forwards the drain's own arguments
        steps.append("drop")
        real_drop(*args)

    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refuse)
    monkeypatch.setattr(outbox, "drop_event", _drop)

    uri = f"file://{tmp_path}/_lineage_outbox"
    _staged(uri, author="mallory", run="bronze$events", token="order-probe")

    class _OrderRepo:
        async def ingest_event(self, _ev: Any) -> None:  # noqa: ANN401
            steps.append("ingest")

        async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
            steps.append("record")

    request = cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    outcome = asyncio.run(reconcile_cron._drain_outbox(request, cast("Any", _OrderRepo()), _governed_settings(uri), {}))

    assert (outcome.refused, outcome.stranded, outcome.drained) == (1, 0, 0), (
        f"a refusal must land on its OWN counter; folding it into `stranded` reports a relay fault that is not happening: {outcome}"
    )
    assert steps == ["record", "drop"], f"the refusal path ran {steps}: the object must be retired only after its verdict is recorded"


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
