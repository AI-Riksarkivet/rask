"""Nothing publishes to a subject on this stream that nothing consumes.

`signal_drained` did exactly that. It published to `ingest.run.{run}.drained` to wake a chunk
workflow suspended on an external event — a design that had ALREADY been replaced (the drain became
an activity, and its return value is how the workflow learns the chunk is done), but the publish was
never deleted.

That is not merely dead code. `ingest.run.*.drained` matches this stream's own `ingest.>` subject
filter, and the stream is WORK_QUEUE retention — where a message is removed only when it is ACKED.
With no consumer anywhere, every chunk of every run left one message on the stream forever.

The general shape is the danger: a publish with no reader looks like a working mechanism, so nobody
questions it, and on a work queue it silently accumulates. These tests pin the specific regression
and the general rule.
"""

from __future__ import annotations

import ast
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src" / "ingest"

#: Subjects this stream is allowed to carry, and who drains each. A new entry here is a claim that
#: something acks it — which is exactly the claim that was false for `.drained`.
#:
#:   ingest.tasks.<run>   -> the Worker's durable pull consumer (`WorkQueue.subscribe`)
#:   dlq.ingest.tasks     -> parked poison. Deliberately UNCONSUMED, and deliberately NOT on this
#:                           stream: `DLQ_SUBJECT` sits outside `ingest.>` precisely so parked units
#:                           accumulate on their own stream where an operator is meant to find them,
#:                           rather than on the work queue where they would block it.
_CONSUMED_PREFIXES = ("ingest.tasks",)


def test_signal_drained_is_GONE_not_merely_unused() -> None:
    """The specific regression, checked as CODE rather than as text.

    A plain `"signal_drained" in source` check fails on the comments that explain the removal — and
    the temptation is then to delete the explanation to make the test pass, which is exactly
    backwards. It is also the weakness that let the estate's earlier `iiif` grep-gate go quiet: a
    literal match answers a different question than "does this call still happen".

    So: walk the AST for a DEFINITION or a CALL of that name. Prose survives; code does not.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            defines = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "signal_drained"
            calls = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "signal_drained"
            if defines or calls:
                offenders.append(f"{path.relative_to(SRC.parent.parent)}:{node.lineno}")

    assert not offenders, f"`signal_drained` is back — it publishes to a WORK_QUEUE subject nothing acks: {offenders}"


def test_no_subject_helper_builds_an_UNCONSUMED_subject_on_this_stream() -> None:
    """The general rule, checked structurally rather than by name.

    Every `*_subject()` helper in `queue.py` names a subject something must drain. `drained_subject`
    was the counter-example, and it read as legitimate precisely because it looked like its sibling
    `unit_subject`. A new one that returns an `ingest.…` subject with no consumer fails here.
    """
    queue = SRC / "queue.py"
    tree = ast.parse(queue.read_text(encoding="utf-8"))

    built: dict[str, str] = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.endswith("_subject")]:
        for node in ast.walk(fn):
            # `f"{SUBJECT_ROOT}.tasks.{run_id}"` — take the literal segment right after the root, which
            # is what decides which consumer (if any) sees the message.
            if isinstance(node, ast.JoinedStr):
                literal = "".join(part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str))
                built[fn.name] = literal.strip(".")

    assert built, "found no `*_subject` helpers — the AST walk is checking nothing"

    unconsumed = [
        f"{name}() -> ingest.{segment}" for name, segment in built.items() if not any(f"ingest.{segment}".startswith(prefix) for prefix in _CONSUMED_PREFIXES)
    ]

    assert not unconsumed, (
        "a subject helper builds a subject on the INGEST stream that nothing drains. The stream is\n"
        "WORK_QUEUE retention, so an unacked message stays forever — a publish with no reader is not a\n"
        "signal, it is a leak. Either give it a consumer or do not publish it.\n  " + "\n  ".join(unconsumed)
    )


def test_the_DLQ_stays_OFF_this_stream() -> None:
    """Poison units are meant to accumulate — that is the point of parking them — so their subject
    must NOT match `ingest.>`, or they would pile up on the WORK QUEUE and consume its budget."""
    from ingest.queue import DLQ_SUBJECT, SUBJECT_ROOT

    assert not DLQ_SUBJECT.startswith(f"{SUBJECT_ROOT}."), (
        f"DLQ_SUBJECT {DLQ_SUBJECT!r} is under {SUBJECT_ROOT!r}: parked units would land on the work "
        f"queue, unacked and permanent, instead of on their own stream"
    )
