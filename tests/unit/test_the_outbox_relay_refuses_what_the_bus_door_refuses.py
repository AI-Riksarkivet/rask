"""Staging an event must not be a way around the gate publishing one has to clear.

FOUR PATHS REACH `repository.ingest_event`, and until now only three of them authorized:

* `api/v1/endpoints/ingest.py:58` — the HTTP door: `enforce_author` then `enforce_output_authz`.
* `services/consumer.py:85` — the bus: `authorize(event)`, DROP on denial.
* `api/v1/endpoints/dlq.py:135` — the replay door: `enforce_output_authz`, and its own comment says
  "same gate as ingest — a caller may only replay a run they were authorized to write in the first place".
* `api/reconcile_cron.py:369` — the outbox relay: **nothing**.

WHY THAT MATTERS IS CONTAINMENT BETWEEN SERVICES, not an outside attacker. `enforce_bus_authz` states
the threat it closes: the bus "is authenticated by the sidecar's shared credential and reads the author
off the payload", so "a producer holding that one token could record any provenance it liked about any
dataset, including a `drop_table` operation on a table it has never seen, which the reconcile sweep then
honours". The outbox is writable by exactly those producers — ingest, maintenance and medallion stage
there through the vended outbox credential (`test_the_outbox_credential_is_vended_to_stagers_only.py`),
while lineage itself holds only `s3:DeleteObject` on the prefix (`minio-scoped-users.yaml`,
`DrainItsOwnOutboxAndNothingElse`). So a stager that cannot get an event past the bus door can stage it
instead, and the relay will ingest it unchecked. The gate is not weakened by the bypass; it is skipped.

NO TEST ASSERTED EITHER WAY BEFORE THIS ONE. `test_the_outbox_drain_needs_no_write_permission.py` reads
like a contradiction and is not: it pins that `drop_event` issues a `DeleteObject` and never a
`PutObject` — an S3/IAM property — and says nothing about authorizing an event's contents.

THE SAME FUNCTION, NOT A SECOND COPY. `enforce_bus_authz` already authorizes AS the subject the producer
stamped, which is the only principal a cron tick has; reimplementing "may you record this" here is how
the two doors would drift, and that drift is the defect this closes rather than repeats.

A REFUSAL STRANDS, IT DOES NOT DROP. The drain already separates a malformed object (poison — dropped,
because it can wedge the drain forever) from a failure (stranded — counted, left staged for the next
tick). A governance refusal is neither malformed nor transient, and destroying the only durable copy of
a committed write's provenance is the wrong answer to "you may not record this": it strands.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from lineage.api import reconcile_cron


def test_the_relay_authorizes_before_it_ingests() -> None:
    """THE GATE. The drain is the fourth ingest path and the only one that admitted anything."""
    body = inspect.getsource(reconcile_cron._drain_outbox)

    assert "enforce_bus_authz" in body, (
        "the outbox relay ingests staged events with no authorization, so a producer that cannot get an "
        "event past the bus door can stage it instead — the gate is skipped, not weakened"
    )
    # The CALL, not the word: `ingest_event` appears in this function's own docstring first, so indexing
    # the bare name compared the gate against a sentence rather than against the statement it guards.
    ingest_at = body.index("repository.ingest_event(")
    assert body.index("await enforce_bus_authz(") < ingest_at, "the check must run BEFORE the event reaches the graph"


def test_a_refused_event_is_stranded_rather_than_dropped() -> None:
    """The drain's two existing outcomes mean different things, and a refusal is the second.

    Poison is DROPPED because a malformed object wedges the drain forever. A refusal is well-formed and
    deterministic: dropping it would destroy the only durable copy of a committed write's provenance to
    answer a governance question. It is counted and left staged.
    """
    body = inspect.getsource(reconcile_cron._drain_outbox)

    assert "PermissionDeniedError" in body, "a refusal must be caught distinctly — it is neither poison nor a transient failure"
    refusal_at = body.index("PermissionDeniedError")
    tail = body[refusal_at : refusal_at + 900]
    assert "stranded" in tail, "a refused event must land on the stranded counter, which already exists for exactly this"
    assert "drop_event" not in tail, "a refusal must NOT delete the staged object — that is the poison path, and this is not poison"


def test_the_cron_can_supply_the_principal_the_gate_needs() -> None:
    """`enforce_bus_authz(event, request, settings)` needs a Request; a cron tick has no caller.

    The Request is a carrier — `enforce_output_authz` and `_is_replay` read the FGA client and the
    repository off `app.state` — so FastAPI injecting one into the handler is what makes the same gate
    usable here. Pinned because dropping the parameter would silently re-open the path.
    """
    assert "request" in inspect.signature(reconcile_cron._on_cron).parameters, "the cron handler must take a Request to thread to the drain"
    assert "request" in inspect.signature(reconcile_cron._drain_outbox).parameters, "the drain needs it to call the shared gate"


def test_a_refusal_is_HANDLED_and_not_a_crash(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal branch, EXECUTED — which is the one thing the three tests above cannot do.

    They read `_drain_outbox`'s source text and assert substrings. That form pins the SHAPE of the
    branch and can say nothing about whether it runs, and this is what it missed: the refusal handler
    logged `author_sub_from_payload(payload)` while `payload` is bound only inside the `except
    ValidationError` branch above it, so reaching a refusal raised `UnboundLocalError`. The handler
    sits inside the drain's per-event `try`, so that escaped to the tick's error boundary and aborted
    the WHOLE drain — every other staged event stayed put, tick after tick.

    Observed on the deployed estate 2026-09-14: `lineage_outbox_drain_failed error="cannot access local
    variable 'payload' where it is not associated with a value"` on EVERY sweep — 18 in 50 minutes —
    with `outbox_drained: 0` while `list_events` confirmed an event was staged. The outbox is what makes
    a committed write's provenance survive its producer; a drain that cannot complete a tick makes it a
    write-only store, which is the failure mode `test_ONE_ungraphable_event_does_not_strand_every_other
    _staged_event` already names for a different cause.

    The GATE's decision is not under test here — `test_the_relay_authorizes_before_it_ingests` owns
    that. What is under test is the drain's handling of a refusal it has already received, which is why
    the gate is replaced wholesale rather than driven through a real FGA.
    """
    import asyncio
    import json as _json
    from types import SimpleNamespace
    from typing import cast

    from lance_namespace import PermissionDeniedError

    from medallion.schemas.events import build_run_event
    from service_kit.lakehouse import outbox

    async def _refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionDeniedError("can_write_data required")

    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refuse)

    uri = f"file://{tmp_path}/_lineage_outbox"
    event = build_run_event(
        operation="ingest_events",
        author="mallory",
        job_namespace="medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=2,
        token="refusal-probe",
    )
    outbox.stage_event(uri, {}, event["run"]["runId"], _json.dumps(event))

    class _Repo:
        def __init__(self) -> None:
            self.ingested: list[str] = []

        async def ingest_event(self, ev: Any) -> None:  # pragma: no cover — a refusal must never reach here
            self.ingested.append(ev.run.run_id)

    class _Settings:
        outbox_uri = uri
        outbox_drain_limit = 500
        dapr_pubsub = "lineage-pubsub"
        dapr_topic = "lineage.events.v1"
        dapr_publish_timeout_seconds = 5.0
        fga_enabled = True  # the whole point: the other executing tests leave this OFF

    repo = _Repo()
    request = cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))

    outcome = asyncio.run(reconcile_cron._drain_outbox(request, cast("Any", repo), cast("Any", _Settings()), {}))

    assert outcome.stranded == 1, "a refused event must be counted as stranded"
    assert outcome.drained == 0 and repo.ingested == [], "a refused event must never reach the graph"
    assert list(outbox.list_events(uri, {})), "a refusal must LEAVE the event staged — dropping it destroys the only durable copy"
