"""A governance refusal is settled, so the staged object must STOP being re-refused every tick.

THE DEFECT, measured live 2026-09-21 and unchanged since 2026-09-14: `s3://lance-catalog/_lineage_outbox`
holds seven `@COMPLETE.json` objects, the sweep reports `drained=0 refused=7` on every tick, and it has
done so for days. The drain is working exactly as designed — the events are well-formed, the answer is
deterministic, and `reconcile_cron` already separates `refused` from `stranded` so a settled governance
answer cannot be read as an outage. What it has no answer for is what happens NEXT: nothing retires the
object, so the same seven are re-read, re-parsed, re-refused and re-logged forever.

WHY THE OBVIOUS REMEDY IS UNAVAILABLE, and the estate already knew: moving the object to a
`<outbox>/_refused/` prefix is a PutObject, and the chart grants this service exactly `s3:DeleteObject`
on `*/_lineage_outbox/*` (statement `DrainItsOwnOutboxAndNothingElse`), pinned by
`test_the_lineage_plane_writes_nothing_it_does_not_own.py`. Building it anyway broke the drain on this
estate: the AccessDenied is the tick's error boundary, so it aborted the WHOLE pass and the sweep then
reported `refused=0` — a zero meaning "did not look". See `docs/DECISIONS.md` and [[LH-004]].

WHAT IS AVAILABLE, and why it needs no new capability. Delete is granted; what the relay lacked was
somewhere to put the evidence first. It has one: this service OWNS its Postgres DDL, creating
`lineage_events` and `lineage_reads` at boot through `repository.ensure_events_table` (`main.py:92`).
So the terminal state is *record the verdict durably, then delete the object* — the row's own
"delete (it may) with the copy preserved beforehand by something that can write", with the writer being
the database this service already provisions.

THE EVIDENCE MUST OUTLIVE THE OBJECT. A refusal is a loss record: someone staged provenance they were
not authorized to record, and the graph will never hold it. Deleting without preserving the event would
turn a governance refusal into silent data loss, which is worse than the loop it replaces.
"""

from __future__ import annotations

from lineage.services import postgres


def test_a_refusal_table_exists_in_the_ddl() -> None:
    """RED before the fix: the service provisioned two tables and neither could hold a verdict.

    Asserted against the DDL constants rather than a live database so the gate runs in the unit layer,
    where the rest of this service's schema is already pinned.
    """
    ddl = [v for k, v in vars(postgres).items() if k.startswith("CREATE_") and isinstance(v, str)]

    assert any("lineage_outbox_refusals" in stmt for stmt in ddl), (
        "no table holds a settled refusal, so the only terminal state available is deleting the evidence"
    )


def test_the_refusal_table_preserves_the_event_and_names_the_key() -> None:
    """The row has to carry BOTH halves or the terminal state loses something the loop preserved.

    The outbox key is what makes the record idempotent across ticks and identifies the object that was
    removed; the event payload is the provenance itself, which no longer exists anywhere else once the
    object is deleted. A record with only one of them is a worse outcome than re-refusing forever.
    """
    stmt = next(v for k, v in vars(postgres).items() if k.startswith("CREATE_") and isinstance(v, str) and "lineage_outbox_refusals" in v)

    assert "outbox_key" in stmt, "without the key the record cannot be matched to the object it retires"
    assert "event" in stmt, "without the event the refusal is data loss, not a loss RECORD"
    assert "reason" in stmt, "a verdict with no reason cannot be acted on by whoever is asked to grant"


def test_the_key_is_unique_so_a_retick_cannot_duplicate_the_record() -> None:
    """A refusal is settled, so recording it twice is a bug the schema should make impossible.

    The drain re-reads the outbox every tick, and a crash between the insert and the delete leaves the
    object in place — so the next tick WILL re-refuse an event already recorded. That path must be an
    idempotent no-op, which needs uniqueness on the key rather than care at the call site.

    ASSERTED AS THE PROPERTY, NOT AS A KEYWORD. The first version of this leg grepped the DDL for the
    literal "unique" and failed against `outbox_key text PRIMARY KEY` — which enforces exactly the
    constraint being asked for, and more strongly than a unique index (it is also NOT NULL). A test that
    can be satisfied by spelling rather than by behaviour is the failure this file exists to avoid, so it
    now checks that the key carries the constraint AND that the write is an upsert, which is where the
    idempotency actually lives.
    """
    ddl = " ".join(v for k, v in vars(postgres).items() if isinstance(v, str) and "lineage_outbox_refusals" in v).lower()

    assert "outbox_key text primary key" in ddl or "unique" in ddl, "the key must be constrained, or a retick appends a second row"
    assert "on conflict (outbox_key) do update" in ddl, "a re-refused event must UPDATE its record; a bare insert raises on the second tick"
