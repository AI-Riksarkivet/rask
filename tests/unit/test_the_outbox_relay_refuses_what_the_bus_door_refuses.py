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

A REFUSAL IS RECORDED BEFORE IT IS RETIRED. The drain separates a malformed object (poison — dropped,
because it can wedge the drain forever) from a failure (stranded — counted, left staged for the next
tick). A governance refusal is neither malformed nor transient, so the verdict and the event go to
`lineage_outbox_refusals` first ([[LH-182]]) and only then is the object dropped: never destroy the only
durable copy of a committed write's provenance to answer "you may not record this".

IT IS COUNTED AS `refused`, NOT `stranded` ([[LH-004]]). `stranded` is documented as "a tick FAILED
while it recovered everything else", and a refusal is the opposite: well-formed, deterministic, and no
evidence of any fault. Counting the two together made `drained=0 stranded=6` — measured unchanged for
2.1 days on a healthy relay — read as a wedged one. THE HANDLING IS UNCHANGED; only the number moved.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from lineage.api import reconcile_cron
from lineage.core.config import LineageSettings


def _governed_settings(outbox_uri: str) -> LineageSettings:
    """The real settings, FGA on — the whole point: the other executing tests leave it off."""
    auth = {"oidc_enabled": True, "oidc_issuer": "https://dex.example", "oidc_audience": "lance", "fga_store_id": "s", "fga_model_id": "m"}
    return LineageSettings.model_validate({"database_url": "postgresql://x/y", "outbox_uri": outbox_uri, "fga_enabled": True, **auth})


def test_the_relay_authorizes_before_it_ingests() -> None:
    """THE GATE. The drain is the fourth ingest path and the only one that admitted anything."""
    body = inspect.getsource(reconcile_cron._drain_outbox)

    assert "enforce_bus_authz" in body, (
        "the outbox relay ingests staged events with no authorization, so a producer that cannot get an "
        "event past the bus door can stage it instead — the gate is skipped, not weakened"
    )
    # The CALLS, not the words: `ingest_event` appears in this function's own docstring first, so indexing
    # the bare name compared the gate against a sentence rather than against the statement it guards. BOTH
    # doors: a catalog DDL change reaches the graph through `ingest_dataset_event`.
    gate_at = body.index("await enforce_bus_authz(")
    for door in ("repository.ingest_event(", "repository.ingest_dataset_event("):
        assert gate_at < body.index(door), f"the check must run BEFORE `{door}` reaches the graph"


def test_a_refused_event_is_counted_apart_and_dropped_only_after_its_record(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The drain's outcomes mean different things, and a refusal is its own.

    Poison is DROPPED because a malformed object wedges the drain forever. A refusal is well-formed and
    deterministic, so it lands on `refused`, never `stranded`, and its object is retired only AFTER the
    verdict and the event are recorded ([[LH-182]]): a drop first would destroy the only durable copy of
    a committed write's provenance. DRIVEN, with the order observed at the two calls themselves.
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

    steps: list[str] = []
    real_drop = outbox.drop_event

    def _drop(*args: Any) -> None:  # noqa: ANN401 — forwards the drain's own arguments
        steps.append("drop")
        real_drop(*args)

    monkeypatch.setattr(reconcile_cron, "enforce_bus_authz", _refuse)
    monkeypatch.setattr(outbox, "drop_event", _drop)

    uri = f"file://{tmp_path}/_lineage_outbox"
    event = build_run_event(
        operation="ingest_events",
        author="mallory",
        job_namespace="medallion",
        inputs=[("bronze", "bronze$events")],
        output_namespace="bronze",
        output_name="bronze$events",
        version=2,
        token="order-probe",
    )
    outbox.stage_event(uri, {}, event["run"]["runId"], _json.dumps(event))

    class _Repo:
        async def ingest_event(self, _ev: Any) -> None:  # noqa: ANN401
            steps.append("ingest")

        async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
            steps.append("record")

    request = cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
    outcome = asyncio.run(reconcile_cron._drain_outbox(request, cast("Any", _Repo()), _governed_settings(uri), {}))

    assert (outcome.refused, outcome.stranded, outcome.drained) == (1, 0, 0), (
        f"a refusal must land on its OWN counter; folding it into `stranded` reports a relay fault that is not happening: {outcome}"
    )
    assert steps == ["record", "drop"], f"the refusal path ran {steps}: the object must be retired only after its verdict is recorded"


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
            self.refusals: list[dict[str, str | None]] = []

        async def ingest_event(self, ev: Any) -> None:  # pragma: no cover — a refusal must never reach here
            self.ingested.append(ev.run.run_id)

        async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
            self.refusals.append({"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event_json": event_json})

    repo = _Repo()
    request = cast("Any", SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))

    outcome = asyncio.run(reconcile_cron._drain_outbox(request, cast("Any", repo), _governed_settings(uri), {}))

    assert outcome.refused == 1, "a refused event must be counted as refused"
    assert outcome.stranded == 0, "`stranded` means a tick that FAILED; a governance refusal is not one"
    assert outcome.drained == 0 and repo.ingested == [], "a refused event must never reach the graph"
    # [[LH-182]] THE PROPERTY IS THE SAME, THE MECHANISM IS NOT. This asserted "leave it staged",
    # because staging was the only durable copy. The drain now writes the verdict AND the event into
    # `lineage_outbox_refusals` first, so retiring the object destroys nothing — and NOT retiring it
    # meant re-reading, re-parsing and re-refusing the same event on every tick forever (measured
    # `refused=7`, unchanged for days). Dropping WITHOUT the record is still the failure, which is why
    # both halves are asserted here and the ordering is pinned in the lineage suite.
    assert repo.refusals and repo.refusals[0]["event_json"], "the refusal was not recorded with its event — retiring the object would be data loss"
    assert not list(outbox.list_events(uri, {})), "a settled refusal left the object staged, so it will be re-refused forever"
