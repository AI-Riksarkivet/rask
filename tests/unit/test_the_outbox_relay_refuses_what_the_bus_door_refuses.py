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
